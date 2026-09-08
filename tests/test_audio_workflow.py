import io
import json
import threading
import time
import wave
from pathlib import Path
import pymupdf
import pytest
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.catalog import Catalog
from b2v.audio_workflow import AudioWorkflow, LongSentence, split_for_tts, join_wavs, wav_info
from b2v.stylebert import TTSSettings, inspect_wav

IDENTITY={'model_id':'voice','weight_sha256':'weights','config_sha256':'config','style_vectors_sha256':'styles'}

def settings():return TTSSettings(model_id='voice',speaker_id=0,style='Neutral')

def audio(value=1):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(value.to_bytes(2,'little')*240)
    return stream.getvalue()

class Voice:
    def __init__(self):self.seen=[];self.fail=False;self.identity=dict(IDENTITY)
    def health(self):return {'status':'ok','engine':'Style-Bert-VITS2'}
    def list_models(self):return {'models':[{'id':'voice','speakers':[{'id':0,'name':'voice'}],'styles':['Neutral'],'identity':self.identity}]}
    def synthesize(self,text,settings):
        if self.fail:raise ValueError('TTS offline')
        self.seen.append(text);data=audio(len(self.seen))
        return data,{'model_identity':dict(self.identity),'tts_seconds':.01,**inspect_wav(data)}

@pytest.fixture
def book(tmp_path):
    c=Catalog(tmp_path);pdf=pymupdf.open();pdf.new_page()
    doc=c.add('book.pdf',io.BytesIO(pdf.tobytes()));ident=doc['id']
    source='これは最初の文です。'*8+'\n\n'+'次の段落を読みます。'*8
    path=c.folder(ident)/'text/book_final.txt';path.write_text(source)
    doc.update(final_current=True);doc['assets']['text']=True;c.save_doc(doc)
    voice=Voice();flow=AudioWorkflow(c,lambda:voice)
    yield c,ident,flow,voice,source
    flow.stop();c.executor.shutdown(wait=True)

def finish(c,ident):
    for _ in range(500):
        if not c.doc(ident).get('busy'):return c.doc(ident)
        time.sleep(.01)
    raise AssertionError('WAV still busy')

def test_split_preserves_text_and_refuses_long_sentence():
    text=('「これは文章です。」'*10)+'\n\n'+('最後の文章です。'*8)
    chunks=split_for_tts(text,50)
    assert ''.join(c['text'] for c in chunks)==text
    assert all(len(c['text'])<=50 for c in chunks)
    assert all(c['text'].endswith(('。','」','\n')) for c in chunks)
    with pytest.raises(LongSentence) as exc:split_for_tts('あ'*51+'。',50)
    assert exc.value.details['characters']==52

def test_generate_resume_fingerprints_and_old_result_retention(book):
    c,ident,flow,voice,source=book
    flow.start(ident,settings(),50);doc=finish(c,ident)
    assert doc['audio_run']['status']=='completed'
    expected=split_for_tts(source,50)
    assert voice.seen==[p['text'] for p in expected]
    result=doc['audio_result'];old=c.folder(ident)/'audio'/result['filename']
    info=wav_info(old);assert info['frames']==240*len(expected)
    with wave.open(str(old)) as wav:
        raw=wav.readframes(info['frames'])
    assert raw==b''.join(i.to_bytes(2,'little')*240 for i in range(1,len(expected)+1))
    flow.start(ident,settings(),50);doc=finish(c,ident)
    assert len(voice.seen)==len(expected) and doc['audio_run']['reused']==len(expected)
    old=c.folder(ident)/'audio'/doc['audio_result']['filename']
    voice.fail=True
    flow.start(ident,settings().model_copy(update={'speed':1.25}),50);doc=finish(c,ident)
    assert doc['audio_run']['status']=='failed' and old.exists()
    voice.fail=False;voice.identity['weight_sha256']='new weights'
    flow.start(ident,settings(),50);doc=finish(c,ident)
    assert doc['audio_run']['reused']==0
    c.invalidate(doc);c.save_doc(doc)
    assert c.doc(ident)['audio_stale']
    with pytest.raises(ValueError,match='最終テキスト'):flow.start(ident,settings(),50)
    c.remove(ident,'audio')
    assert not c.doc(ident).get('audio_result')
    assert (c.folder(ident)/'text/book_final.txt').exists()
    with c.connect() as db:assert db.execute('SELECT count(*) FROM audio_chunks').fetchone()[0]==0

def test_stop_after_current_chunk_and_restart_reuses(book):
    c,ident,flow,voice,source=book
    entered=threading.Event();release=threading.Event();original=voice.synthesize
    def blocking(*args):entered.set();assert release.wait(5);return original(*args)
    voice.synthesize=blocking
    flow.start(ident,settings(),50)
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError):flow.start(ident,settings(),50)
        with pytest.raises(ValueError):c.remove(ident,'audio')
        flow.stop(ident)
    finally:release.set()
    doc=finish(c,ident)
    assert doc['audio_run']['status']=='stopped' and len(voice.seen)==1
    voice.synthesize=original
    reopened=AudioWorkflow(c,lambda:voice)
    reopened.start(ident,settings(),50);doc=finish(c,ident)
    assert doc['audio_run']['status']=='completed' and doc['audio_run']['reused']>=1

def test_changed_text_and_corrupt_cache_not_reused(book):
    c,ident,flow,voice,source=book
    flow.start(ident,settings(),50);finish(c,ident)
    paths=list((c.folder(ident)/'audio/chunks').glob('*.wav'));paths[0].write_bytes(b'bad')
    before=len(voice.seen)
    flow.start(ident,settings(),50);finish(c,ident)
    assert len(voice.seen)>before
    (c.folder(ident)/'text/book_final.txt').write_text('違う文です。'*8)
    flow.start(ident,settings(),50);doc=finish(c,ident)
    assert doc['audio_run']['reused']==0

def test_long_sentence_record_and_sample_removal(book):
    c,ident,flow,voice,_=book
    path=c.folder(ident)/'audio/sample_old.wav';path.write_bytes(audio())
    doc=c.doc(ident);doc.update(tts_sample={'filename':path.name},tts_sample_text='trial',tts_settings=settings().model_dump());c.save_doc(doc)
    flow.remove_samples()
    assert not path.exists() and 'tts_sample' not in c.doc(ident)
    assert c.doc(ident)['tts_settings']==settings().model_dump()
    (c.folder(ident)/'text/book_final.txt').write_text('あ'*301+'。')
    with pytest.raises(LongSentence):flow.start(ident,settings())
    assert c.doc(ident)['audio_run']['long_sentence']['characters']==302
    assert not voice.seen

def test_api_wav_settings_and_download(tmp_path,monkeypatch):
    voice=Voice()
    with TestClient(create_app(tmp_path)) as client:
        app=client.app;app.state.audio.client_factory=lambda:voice
        pdf=pymupdf.open();pdf.new_page()
        ident=client.post('/api/documents',files={'file':('本.pdf',pdf.tobytes(),'application/pdf')}).json()['id']
        base='/api/documents/'+ident
        body={'settings':settings().model_dump(),'max_chunk_chars':50}
        assert client.put(base+'/tts',json=body).status_code==200
        assert client.post(base+'/tts/wav',json=body).status_code==400
        c=app.state.catalog;doc=c.doc(ident);doc['final_current']=True;c.save_doc(doc)
        (c.folder(ident)/'text/book_final.txt').write_text('本文です。'*12)
        assert client.post(base+'/tts/wav',json=body).status_code==202
        finish(c,ident)
        response=client.get(base+'/tts/wav?download=true')
        assert response.status_code==200 and 'book.wav' in response.headers['content-disposition']
        assert inspect_wav(response.content)['sample_rate']==24000
        assert client.get(base+'/tts').json()['result']['chunks']==2
        assert client.get(base+'/tts/sample').status_code==404


def test_failed_chunk_context_is_saved(book):
    c,ident,flow,voice,source=book
    c.save_page(ident,{'page_number':70,'included':True})
    c.text_path(ident,70).write_text(source)
    voice.fail=True
    flow.start(ident,settings(),100);finish(c,ident)
    run=c.doc(ident)['audio_run']
    assert run['status']=='failed'
    assert run['error_context']['pages']==[70]
    assert run['error_context']['chunk']==1
    assert run['error_context']['text']==split_for_tts(source,100)[0]['text']
