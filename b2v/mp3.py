"""Completed MP3 per document; WAV and chunks are disposable work files."""
import json
import re
import shutil
import subprocess
import time
import uuid
from .catalog import now
from .audio_workflow import wav_info


def executable(name):
    path = shutil.which(name)
    if not path:
        raise ValueError(f'{name}がインストールされていません。MP3生成にはFFmpegとFFprobeが必要です。')
    return path


def command(args, message):
    result = subprocess.run(args, capture_output=True, text=True, timeout=7200)
    if result.returncode:
        raise ValueError(message + ': ' + result.stderr[-2000:])
    return result.stdout


class MP3Workflow:
    def __init__(self, catalog):
        self.catalog = catalog
        for doc in catalog.list():
            if doc.get('mp3_run', {}).get('status') == 'generating':
                doc['mp3_run'].update(status='failed', error='前回のMP3生成が中断されました。再実行してください。')
                catalog.save_doc(doc)
            folder = catalog.folder(doc['id'])/'audio'
            for tomb in folder.glob('*.mp3.deleting'):
                original = tomb.with_suffix('')
                if doc.get('mp3_result', {}).get('filename') == original.name:tomb.replace(original)
                else:tomb.unlink(missing_ok=True)
            if doc.get('mp3_cleanup'):
                self.cleanup(doc['id'])
            folder = catalog.folder(doc['id'])/'audio'
            current = doc.get('mp3_result', {}).get('filename')
            for path in folder.glob('mp3_*.mp3'):
                if path.name != current:
                    path.unlink(missing_ok=True)

    def start(self, ident):
        executable('ffmpeg'); executable('ffprobe')
        with self.catalog.lock:
            doc = self.catalog.idle(ident)
            if doc.get('mp3_cleanup'):
                self.cleanup(ident)
                doc = self.catalog.doc(ident)
                if doc.get('mp3_cleanup'):
                    raise ValueError('前回の作業ファイル削除が完了していません。')
            wav = doc.get('audio_result')
            if not wav or not (self.catalog.folder(ident)/'audio'/wav['filename']).is_file():
                raise ValueError('入力WAVなし。先にWAVを生成してください。')
            doc['mp3_run'] = {'status':'generating', 'started_at':now()}
            self.catalog.save_doc(doc)
            self.catalog.run(ident, 'MP3', lambda:self.generate(ident, wav))

    def cleanup(self, ident):
        with self.catalog.lock:
            doc = self.catalog.doc(ident)
            pending = doc.get('mp3_cleanup', [])
            folder = self.catalog.folder(ident)/'audio'
            failed = []
            for name in pending:
                try:
                    path = folder/name
                    if path.is_dir():shutil.rmtree(path)
                    else:path.unlink(missing_ok=True)
                except OSError:failed.append(name)
            doc['mp3_cleanup'] = failed
            if not failed:
                doc.pop('audio_result', None); doc.pop('audio_run', None)
                with self.catalog.connect() as db:
                    db.execute('DELETE FROM audio_chunks WHERE document_id=?', (ident,))
            doc['mp3_run']['warning'] = 'MP3完成・作業ファイルの削除に失敗しました。再起動時に再試行します。' if failed else ''
            self.catalog.save_doc(doc)

    def generate(self, ident, wav):
        folder = self.catalog.folder(ident)/'audio'
        temporary = folder/('mp3_'+uuid.uuid4().hex+'.mp3')
        committed = False
        started = time.monotonic()
        stage = '入力WAV破損'
        try:
            source = folder/wav['filename']
            info = wav_info(source)
            doc = self.catalog.doc(ident)
            title = re.sub(r'\.pdf$', '', doc['name'], flags=re.I)
            args = [executable('ffmpeg'), '-nostdin', '-v', 'error', '-i', str(source), '-map', '0:a:0', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '96k', '-metadata', 'title='+title]
            if doc.get('author'):args += ['-metadata', 'artist='+doc['author']]
            stage = 'FFmpeg変換失敗'
            command(args+[str(temporary)], stage)
            stage = '出力MP3検証失敗'
            probe = json.loads(command([executable('ffprobe'), '-v','error','-show_streams','-show_format','-of','json',str(temporary)], stage))
            stream = probe['streams'][0]; duration = float(probe['format']['duration'])
            if stream['codec_name']!='mp3' or stream['channels']!=1 or abs(duration-info['duration'])>1 or int(stream['bit_rate'])!=96000:
                raise ValueError('形式または再生時間が一致しません。')
            command([executable('ffmpeg'),'-nostdin','-v','error','-xerror','-i',str(temporary),'-f','null','-'],stage)
            result = {'filename':temporary.name, 'display_name':re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', title).strip(' .')[:180]+'.mp3', 'duration':duration, 'sample_rate':int(stream['sample_rate']), 'channels':1, 'bitrate_kbps':96, 'size_bytes':temporary.stat().st_size, 'generated_at':now()}
            stage = 'SQLite登録失敗'
            with self.catalog.lock:
                doc = self.catalog.doc(ident)
                old = doc.get('mp3_result')
                doc['mp3_result'] = result
                doc['mp3_run'] = {'status':'completed','finished_at':now(),'seconds':time.monotonic()-started}
                doc['mp3_cleanup'] = [wav['filename'],'chunks'] + ([old['filename']] if old else [])
                doc['assets']['audio'] = True
                self.catalog.save_doc(doc)
                committed = True
            self.cleanup(ident)
        except Exception as exc:
            with self.catalog.lock:
                doc = self.catalog.doc(ident)
                if committed:doc['mp3_run']['warning'] = 'MP3完成・後片付けに失敗しました: '+str(exc)
                else:doc['mp3_run'] = {'status':'failed','error':stage+': '+str(exc)}
                self.catalog.save_doc(doc)
        finally:
            if not committed:temporary.unlink(missing_ok=True)
            with (folder/'mp3.log.jsonl').open('a') as log:
                log.write(json.dumps({'document_id':ident,'input':wav['filename'],'output':temporary.name,'seconds':time.monotonic()-started,'result':self.catalog.doc(ident).get('mp3_result') if committed else None,**self.catalog.doc(ident)['mp3_run']},ensure_ascii=False)+'\n')

    def delete(self, ident):
        with self.catalog.lock:
            doc = self.catalog.idle(ident)
            result = doc.get('mp3_result')
            path = self.catalog.folder(ident)/'audio'/result['filename'] if result else None
            tomb = path.with_suffix('.mp3.deleting') if path else None
            if path and path.exists():path.replace(tomb)
            doc.pop('mp3_result', None)
            doc.pop('mp3_run', None)
            doc['assets']['audio'] = bool(doc.get('audio_result') or doc.get('audio_run'))
            try:self.catalog.save_doc(doc)
            except Exception:
                if tomb and tomb.exists():tomb.replace(path)
                raise
            if tomb:tomb.unlink(missing_ok=True)
            self.catalog.prune_empty(ident)
