"""Human-triggered, document-linked WAV generation with resumable chunks."""
import hashlib
import json
import logging
import re
import threading
import time
import uuid
import wave
from pathlib import Path

from .catalog import now
from .stylebert import StyleBertClient, TTSSettings

logger = logging.getLogger('uvicorn.error')
SPLITTER_VERSION = 'paragraph-sentence-v1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',',':')).encode())


class LongSentence(ValueError):
    def __init__(self, text, start, end, maximum):
        super().__init__('音声化できない長文があります。本文を修正し、最終テキストを再生成してください。')
        self.details = {'text':text[start:end], 'start':start, 'end':end,
                        'characters':end-start, 'maximum':maximum}


def split_for_tts(text, maximum=300):
    """Preserve the input exactly; never force-split a sentence."""
    if not 50 <= maximum <= 300:
        raise ValueError('最大文字数は50〜300で指定してください。')
    if not text.strip():
        raise ValueError('最終テキストに音声化する本文がありません。')
    paragraphs = {m.end() for m in re.finditer(r'\n[ \t]*\n+', text)}
    sentences = {m.end() for m in re.finditer(r'[。！？!?]+[」』”’）】]*', text)}
    boundaries = sorted(paragraphs | sentences | {len(text)})
    result = []
    start = 0
    while start < len(text):
        if len(text)-start <= maximum:
            end = len(text)
        else:
            preferred = [p for p in paragraphs if start < p <= start+maximum]
            fitting = [p for p in boundaries if start < p <= start+maximum]
            if not fitting:
                end = next(p for p in boundaries if p > start)
                raise LongSentence(text, start, end, maximum)
            end = max(preferred or fitting)
        result.append({'index':len(result)+1, 'start':start, 'end':end, 'text':text[start:end]})
        start = end
    return result


def wav_info(path):
    with wave.open(str(path), 'rb') as wav:
        rate, channels, width, frames = wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()
        if not rate or not frames or channels!=1 or width!=2 or wav.getcomptype()!='NONE':
            raise ValueError('音声形式が不正です（mono PCM16が必要）。')
        count = 0
        while block := wav.readframes(65536):
            count += len(block)
        if count != frames*channels*width:
            raise ValueError('WAVファイルが途中で切れています。')
    return {'sample_rate':rate, 'channels':channels, 'sample_width':width,
            'frames':frames, 'duration':frames/rate, 'size_bytes':path.stat().st_size}


def file_hash(path):
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            sha.update(block)
    return sha.hexdigest()


def join_wavs(paths, output, stop=None):
    """Inputs from one model already share a format; verify before streaming."""
    infos = [wav_info(p) for p in paths]
    if not infos:
        raise ValueError('結合する音声がありません。')
    fmt = tuple(infos[0][k] for k in ('sample_rate','channels','sample_width'))
    if any(tuple(i[k] for k in ('sample_rate','channels','sample_width'))!=fmt for i in infos):
        raise ValueError('モデルの音声形式が途中で変わりました。異なる形式のWAVは結合しません。')
    total_frames = sum(i['frames'] for i in infos)
    if total_frames*fmt[1]*fmt[2] > 0xFFFFFFFF-36:
        raise ValueError('1つのWAVで保存できる4GB上限を超えます。')
    with wave.open(str(output), 'wb') as out:
        out.setframerate(fmt[0]); out.setnchannels(fmt[1]); out.setsampwidth(fmt[2])
        out.setnframes(total_frames)
        for path in paths:
            if stop and stop.is_set():
                raise InterruptedError('停止しました。')
            with wave.open(str(path), 'rb') as source:
                while block := source.readframes(65536):
                    out.writeframesraw(block)
    return wav_info(output)


class AudioWorkflow:
    def __init__(self, catalog, client_factory=StyleBertClient):
        self.catalog = catalog
        self.client_factory = client_factory
        self.guard = threading.Lock()
        self.active = None
        self.stop_event = threading.Event()
        with catalog.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS audio_chunks (document_id TEXT, fingerprint TEXT, data TEXT NOT NULL, PRIMARY KEY(document_id,fingerprint))')
            for doc in catalog.list():
                run = doc.get('audio_run')
                if run and run['status'] in ('generating','joining','queued'):
                    run.update(status='interrupted', error='前回の音声生成が中断されました。再開できます。')
                    catalog.save_doc(doc)

    def update(self, ident, **changes):
        with self.catalog.lock:
            doc = self.catalog.doc(ident)
            doc.setdefault('audio_run',{}).update(changes)
            self.catalog.save_doc(doc)

    def stop(self, ident=None):
        with self.guard:
            if self.active and (ident is None or self.active==ident):
                self.stop_event.set()
                self.update(self.active, stop_requested=True)

    def remove_samples(self):
        """Retire audition artifacts through document management, keep voice settings."""
        with self.catalog.lock:
            for doc in self.catalog.list():
                if doc.get('tts_sample_status')=='generating':
                    raise ValueError('サンプル生成が完了してから更新してください。')
                folder = self.catalog.folder(doc['id'])/'audio'
                for path in folder.glob('sample_*.wav'):
                    path.unlink()
                for key in ('tts_sample','tts_sample_text','tts_sample_status','tts_sample_error'):
                    doc.pop(key,None)
                doc['assets']['audio'] = folder.exists() and any(p.is_file() and p.suffix in ('.wav','.mp3') for p in folder.rglob('*'))
                self.catalog.save_doc(doc)

    def start(self, ident, settings, maximum=300):
        with self.guard:
            if self.active:
                raise ValueError('別のWAV生成が実行中です。完了または停止してから開始してください。')
            with self.catalog.lock:
                doc = self.catalog.idle(ident)
                if doc.get('mp3_cleanup'):raise ValueError('MP3作業ファイルの削除が未完了です。アプリを再起動してください。')
                path = self.catalog.folder(ident)/'text/book_final.txt'
                if not doc.get('final_current') or not path.exists():
                    raise ValueError('先に現在の本文から最終テキストを生成してください。')
                text = path.read_text(encoding='utf-8')
                try:
                    chunks = split_for_tts(text, maximum)
                except LongSentence as exc:
                    # Locate the source page without rewriting or using it for synthesis.
                    cursor = 0
                    for page in self.catalog.pages(ident):
                        if not page['included']:continue
                        content = self.catalog.text(ident,page['page_number']) or ''
                        if cursor <= exc.details['start'] < cursor+len(content)+2:
                            exc.details['page_number'] = page['page_number'];break
                        cursor += len(content)+2
                    doc['audio_run'] = {'status':'failed','error':str(exc),'long_sentence':exc.details,'processed':0,'total':0}
                    self.catalog.save_doc(doc)
                    raise
                doc['tts_settings'] = settings.model_dump()
                doc['tts_max_chars'] = maximum
                doc['operation_total'] = len(chunks)
                doc['audio_run'] = {'status':'queued','processed':0,'total':len(chunks),'reused':0,
                                    'error':None,'stop_requested':False,'started_at':now(),'long_sentence':None}
                self.catalog.save_doc(doc)
                self.stop_event.clear();self.active = ident
                try:
                    return self.catalog.run(ident,'WAV',lambda:self.generate(ident,text,chunks,settings,maximum))
                except Exception:
                    self.active = None;raise

    def cache(self, ident, key):
        with self.catalog.connect() as db:
            row = db.execute('SELECT data FROM audio_chunks WHERE document_id=? AND fingerprint=?',(ident,key)).fetchone()
        if not row:return None
        data = json.loads(row[0]);path = self.catalog.folder(ident)/'audio/chunks'/data['filename']
        try:
            if not path.is_file() or file_hash(path)!=data['wav_sha256']:return None
            wav_info(path)
        except (OSError, EOFError, wave.Error, ValueError):return None
        return data

    def log(self, ident, info):
        path = self.catalog.folder(ident)/'audio/generation.jsonl'
        path.parent.mkdir(exist_ok=True)
        with path.open('a',encoding='utf-8') as out:
            out.write(json.dumps({'timestamp':now(),**info},ensure_ascii=False)+'\n')

    def generate(self, ident, text, chunks, settings, maximum):
        temp = None
        try:
            if self.stop_event.is_set():raise InterruptedError()
            client = self.client_factory()
            client.health()
            models = client.list_models()['models']
            model = next((m for m in models if m['id']==settings.model_id),None)
            if not model:raise ValueError('指定したModelがありません。')
            if settings.speaker_id not in [s['id'] for s in model['speakers']] or settings.style not in model['styles']:
                raise ValueError('Voice / Styleを確認してください。')
            identity = model['identity']
            common = {'settings':settings.model_dump(),'model_identity':identity,'splitter':SPLITTER_VERSION,
                      'max_chunk_chars':maximum,'pause':'stylebert-default-only'}
            source_hash = digest(text.encode('utf-8'))
            folder = self.catalog.folder(ident)/'audio'
            (folder/'chunks').mkdir(parents=True,exist_ok=True)
            self.update(ident,status='generating',source_sha256=source_hash,settings=settings.model_dump(),model_identity=identity,
                        plan=[{'index':c['index'],'start':c['start'],'end':c['end'],
                               'fingerprint':fingerprint({**common,'text_sha256':digest(c['text'].encode('utf-8'))})} for c in chunks])
            self.log(ident,{'event':'start','source_sha256':source_hash,'total_chars':len(text),'chunks':len(chunks),**common})
            paths = [];reused = 0;tts_seconds = 0
            for chunk in chunks:
                if self.stop_event.is_set():raise InterruptedError()
                if not chunk['text'].strip():continue
                chunk_hash = digest(chunk['text'].encode('utf-8'))
                key = fingerprint({**common,'text_sha256':chunk_hash})
                cached = self.cache(ident,key)
                if cached:
                    info = cached;reused += 1
                else:
                    self.update(ident,current_chunk=chunk['index'])
                    data, info = client.synthesize(chunk['text'],settings)
                    if info['model_identity'] != identity:
                        raise ValueError('生成中にモデルが変更されました。同じモデルで再開してください。')
                    filename = key+'.wav';path = folder/'chunks'/filename
                    temporary = path.with_suffix('.tmp')
                    temporary.write_bytes(data)
                    try:wav_info(temporary);temporary.replace(path)
                    finally:temporary.unlink(missing_ok=True)
                    info = {**info,'filename':filename,'fingerprint':key,'text_sha256':chunk_hash,
                            'text_length':len(chunk['text']),'wav_sha256':digest(data),'status':'completed'}
                    with self.catalog.connect() as db:
                        db.execute('INSERT OR REPLACE INTO audio_chunks VALUES (?,?,?)',(ident,key,json.dumps(info,ensure_ascii=False)))
                    tts_seconds += info.get('tts_seconds',0)
                paths.append(folder/'chunks'/info['filename'])
                self.log(ident,{'event':'chunk','index':chunk['index'],'fingerprint':key,'reused':bool(cached),
                                'duration':info['duration'],'tts_seconds':info.get('tts_seconds',0)})
                with self.catalog.lock:
                    doc = self.catalog.doc(ident);doc['assets']['audio'] = True
                    doc['processed'] = chunk['index'];doc['operation_total'] = len(chunks)
                    doc['audio_run'].update(processed=chunk['index'],reused=reused,tts_seconds=tts_seconds)
                    self.catalog.save_doc(doc)
            if self.stop_event.is_set():raise InterruptedError()
            if len(paths)!=sum(bool(c['text'].strip()) for c in chunks):raise ValueError('欠落chunkがあります。')
            self.update(ident,status='joining')
            temp = folder/('book_'+uuid.uuid4().hex+'.tmp')
            info = join_wavs(paths,temp,self.stop_event)
            if self.stop_event.is_set():raise InterruptedError()
            filename = temp.with_suffix('.wav').name
            with self.catalog.lock:
                doc = self.catalog.doc(ident);old = doc.get('audio_result')
                result = {**info,**common,'filename':filename,'generated_at':now(),'source_sha256':source_hash,
                          'tts_seconds':tts_seconds,'total_chars':len(text),'chunks':len(paths)}
                temp.replace(folder/filename);temp = folder/filename
                doc.update(audio_result=result,audio_stale=False)
                doc['audio_run'].update(status='completed',finished_at=now(),processed=len(chunks))
                doc['assets']['audio'] = True
                self.catalog.save_doc(doc);temp = None
                if old and old['filename']!=filename:
                    try:(folder/old['filename']).unlink(missing_ok=True)
                    except OSError:logger.warning('Old WAV cleanup failed: %s',old['filename'])
            self.log(ident,{'event':'completed',**result})
        except InterruptedError:
            self.update(ident,status='stopped',error=None,stop_requested=False)
        except Exception as exc:
            self.update(ident,status='failed',error=str(exc))
            logger.exception('WAV generation failed: %s',ident)
        finally:
            if temp:temp.unlink(missing_ok=True)
            with self.guard:self.active = None
