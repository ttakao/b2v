"""Manage only native processes started by run-all; never force-kill."""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
import httpx
from .config import data_dir, service_url
from .launch import command

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT/'run'
LOG = ROOT/'logs'
SERVICES = [('api','b2v'),('llm','llm'),('stylebert','stylebert')]


def identity(pid):
    result = subprocess.run(['ps','-p',str(pid),'-o','lstart=','-o','command='],capture_output=True,text=True)
    if result.returncode!=0:return None
    parts=result.stdout.strip().split(maxsplit=6)
    if len(parts)<7:return None
    # macOS Python can replace argv[0] with its Framework executable during startup.
    # Creation time and the full argument string remain stable across that change.
    return ' '.join(parts[:5])+' | '+parts[6]


def owned(record):
    return bool(record.get('identity')) and identity(record['pid'])==record['identity']


def listening(url):
    parsed=urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname,parsed.port or 80),timeout=1):return True
    except OSError:return False


def ready(service,url):
    path='/api/documents/storage' if service=='api' else '/health'
    try:
        response=httpx.get(url+path,timeout=2,trust_env=False)
        if response.status_code!=200:return False
        data=response.json()
        if service=='api':return data.get('root')==str(data_dir())
        if service=='stylebert':return data.get('engine')=='Style-Bert-VITS2' and data.get('status')=='ok'
        return data.get('status')=='ok'
    except (httpx.HTTPError,ValueError):return False


def record_path(name):return RUN/(name+'.json')


def read_record(name):
    path=record_path(name)
    if not path.exists():return None
    return json.loads(path.read_text())


def start_one(service,name):
    url=service_url(service)
    record=read_record(name)
    if record and owned(record):
        if record['url']!=url:raise RuntimeError(f'{name}: 起動中のURLは {record["url"]} です。設定変更前に停止してください。')
        if not ready(service,url):raise RuntimeError(f'{name}: 管理中のプロセスはありますが応答を確認できません。ログを確認してください。')
        print(f'{name}: 起動済み ({url})',flush=True);return
    if listening(url):
        if ready(service,url):
            print(f'{name}: 別の方法で起動済み ({url})。停止管理の対象外です。',flush=True);return
        raise RuntimeError(f'{name}: {url} は別プロセスが使用中、またはサービス準備中です。')
    args=command(service)
    env=os.environ.copy()
    if service=='stylebert':env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    cwd=ROOT/'stylebert/sbv2-src' if service=='stylebert' else ROOT
    logfile=LOG/(name+'.log')
    with logfile.open('ab') as stream:
        process=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
    # Popen has completed exec; capture executable command and process creation time.
    record={'pid':process.pid,'identity':identity(process.pid),'url':url,'service':service}
    temporary=record_path(name).with_suffix('.tmp')
    temporary.write_text(json.dumps(record));temporary.replace(record_path(name))
    (RUN/(name+'.pid')).write_text(str(process.pid)+'\n')
    print(f'{name}: 起動確認中… PID {process.pid} / {logfile}',flush=True)
    deadline=time.monotonic()+180
    last=time.monotonic()
    while time.monotonic()<deadline:
        if process.poll() is not None:raise RuntimeError(f'{name}: 起動失敗。ログ: {logfile}')
        if ready(service,url):
            print(f'{name}: 利用可能 ({url})',flush=True);return
        if time.monotonic()-last>=15:
            print(f'{name}: 初期化を待っています…',flush=True);last=time.monotonic()
        time.sleep(1)
    raise RuntimeError(f'{name}: 起動確認がタイムアウトしました。プロセスは保持しています。ログ確認または ./stop-all.sh を実行してください。')


def stop_one(name):
    record=read_record(name)
    if not record:
        print(f'{name}: このスクリプトの起動記録なし。停止しません。',flush=True);return
    if owned(record):
        print(f'{name}: 通常終了を要求します (PID {record["pid"]})',flush=True)
        try:os.kill(record['pid'],signal.SIGTERM)
        except ProcessLookupError:pass
        last=time.monotonic()
        while owned(record):
            if time.monotonic()-last>=10:
                print(f'{name}: 処理終了を待っています（強制終了はしません）…',flush=True);last=time.monotonic()
            time.sleep(1)
    else:print(f'{name}: 停止済み、またはPIDが別プロセスです。シグナルは送りません。',flush=True)
    record_path(name).unlink(missing_ok=True)
    (RUN/(name+'.pid')).unlink(missing_ok=True)
    print(f'{name}: 停止確認済み',flush=True)


def main(action):
    RUN.mkdir(exist_ok=True);LOG.mkdir(exist_ok=True)
    with (RUN/'services.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('別の起動・停止操作が実行中です。')
        if action=='start':
            for service,name in SERVICES:start_one(service,name)
            print(f'起動確認完了\nDATA: {data_dir()}\nb2v: {service_url("api")}',flush=True)
        else:
            record=read_record('b2v')
            url=record['url'] if record else service_url('api')
            stop_one('b2v')
            if listening(url):raise RuntimeError('管理対象外のb2vが稼働中です。先にそのb2vを終了してください。LLM・Style-Bertは保持します。')
            stop_one('llm');stop_one('stylebert')
            print('停止確認完了',flush=True)


if __name__=='__main__':
    try:main(sys.argv[1])
    except KeyboardInterrupt:
        print('\n操作を中断しました。サービスは強制終了していません。再度 ./stop-all.sh で停止できます。',file=sys.stderr);sys.exit(130)
    except Exception as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
