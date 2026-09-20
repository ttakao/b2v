import base64
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import wave

import httpx
import pytest
import pymupdf
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v import google_tts
from b2v.google_tts import GoogleClient, GoogleSettings, Usage
from b2v.tts_api import WAVRequest
from test_audio_workflow import book, finish


def wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
        audio.writeframes(b'\x01\x00'*2400)
    return out.getvalue()


def client(root, seen, status=200):
    def handle(request):
        assert request.headers['x-goog-user-project']=='text2voice-509213'
        assert request.headers['authorization']=='Bearer fake'
        if request.method=='GET':
            return httpx.Response(200, json={'voices':[{'name':'ja-JP-Neural2-C'},{'name':'ja-JP-Neural2-D'}]})
        seen.append(json.loads(request.content))
        return httpx.Response(status, json={'audioContent':base64.b64encode(wav()).decode()})
    return GoogleClient(root, httpx.MockTransport(handle), token_provider=lambda:'fake')


def test_google_mapping_and_reject_retired_settings(tmp_path,monkeypatch):
    monkeypatch.setenv('B2V_GOOGLE_PROJECT','text2voice-509213')
    seen=[];engine=client(tmp_path,seen)
    assert engine.health()['status']=='ok'
    config=GoogleSettings(speed=1.15,pitch=-2)
    data,info=engine.synthesize('日本語。',config)
    assert data==wav() and info['model_identity']['engine']=='google-neural2'
    assert seen[0]['audioConfig']=={'audioEncoding':'LINEAR16','sampleRateHertz':24000,'speakingRate':1.15,'pitch':-2}
    assert seen[0]['voice']['name']=='ja-JP-Neural2-C'
    assert engine.usage.status()['used']==4
    with pytest.raises(ValueError):WAVRequest(settings={'model_id':'jvnv-F1-jp','noise':0.6})
    assert isinstance(WAVRequest(settings=config.model_dump()).settings,GoogleSettings)
    with pytest.raises(ValueError):GoogleSettings(pitch=21)
    with pytest.raises(ValueError):GoogleSettings(model_id='ja-JP-Chirp3-HD-Orus')


def test_cap_failed_request_no_retry_and_persistence(tmp_path,monkeypatch):
    monkeypatch.setenv('B2V_GOOGLE_MONTHLY_LIMIT','6')
    seen=[];engine=client(tmp_path,seen,status=503)
    with pytest.raises(ValueError,match='503'):engine.synthesize('本文。',GoogleSettings())
    assert len(seen)==1 and Usage(tmp_path).status()['used']==3
    engine=client(tmp_path,seen)
    engine.synthesize('本文。',GoogleSettings())
    with pytest.raises(ValueError,match='月間上限'):engine.synthesize('次。',GoogleSettings())
    assert len(seen)==2 and Usage(tmp_path).status()['used']==6


def test_concurrent_reservations_month_and_external_import(tmp_path,monkeypatch):
    monkeypatch.setenv('B2V_GOOGLE_MONTHLY_LIMIT','10')
    monkeypatch.setattr(google_tts,'billing_month',lambda:'2026-09')
    usage=Usage(tmp_path)
    usage.record_external('standalone','2026-09',2)
    usage.record_external('standalone','2026-09',2)
    def reserve():
        try:Usage(tmp_path).reserve(6);return True
        except ValueError:return False
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:reserve(),range(2)))
    assert sum(results)==1 and usage.status()['used']==8
    monkeypatch.setattr(google_tts,'billing_month',lambda:'2026-10')
    assert usage.status()['used']==0


def test_google_stop_resume_estimate_and_pitch_invalidation(book,monkeypatch):
    c,ident,flow,_,source=book
    monkeypatch.setenv('B2V_GOOGLE_MONTHLY_LIMIT','900000')
    seen=[];engine=client(c.root,seen);flow.client_factory=lambda:engine
    config=GoogleSettings()
    initial=flow.estimate(ident,config,50)
    assert initial['send_characters']>0
    entered=threading.Event();release=threading.Event();original=engine.synthesize
    def blocking(*args):entered.set();assert release.wait(5);return original(*args)
    engine.synthesize=blocking
    flow.start(ident,config,50)
    try:
        assert entered.wait(5);flow.stop(ident)
    finally:release.set()
    assert finish(c,ident)['audio_run']['status']=='stopped'
    partial=flow.estimate(ident,config,50)
    assert partial['send_characters']<initial['send_characters']
    engine.synthesize=original
    flow.start(ident,config,50)
    assert finish(c,ident)['audio_run']['status']=='completed'
    used=engine.usage.status()['used']
    assert used==initial['send_characters']
    assert flow.estimate(ident,config,50)['send_characters']==0
    flow.start(ident,config,50);finish(c,ident)
    assert engine.usage.status()['used']==used
    assert flow.estimate(ident,GoogleSettings(pitch=-1),50)['send_characters']==initial['send_characters']
    c.remove(ident,'audio')
    assert engine.usage.status()['used']==used


def test_whole_book_over_cap_sends_nothing(book,monkeypatch):
    c,ident,flow,_,_=book
    monkeypatch.setenv('B2V_GOOGLE_MONTHLY_LIMIT','1')
    seen=[];flow.client_factory=lambda:client(c.root,seen)
    flow.start(ident,GoogleSettings(),50)
    assert finish(c,ident)['audio_run']['status']=='failed'
    assert not seen and Usage(c.root).status()['used']==0


def test_google_settings_api_and_audition(tmp_path):
    with TestClient(create_app(tmp_path)) as api:
        seen=[];engine=client(api.app.state.catalog.root,seen)
        api.app.state.audio.client_factory=lambda:engine
        config=GoogleSettings(speed=1.1,pitch=-.5).model_dump()
        response=api.post('/api/tts/audition',json={'text':'試聴です。','settings':config})
        assert response.status_code==200 and response.content==wav()
        assert api.get('/api/tts/usage').json()['used']==5
        assert api.get('/api/tts/models').json()['defaults']['model_id']=='ja-JP-Neural2-C'
        assert api.post('/api/tts/audition',json={'text':'試聴。','settings':{**config,'pitch':99}}).status_code==422
        pdf=pymupdf.open();pdf.new_page()
        ident=api.post('/api/documents',files={'file':('test.pdf',pdf.tobytes(),'application/pdf')}).json()['id']
        base=f'/api/documents/{ident}/tts'
        assert api.put(base,json={'settings':config}).status_code==200
        assert api.get(base).json()['settings']==config


def test_timeout_counts_once_and_oversized_text_never_sent(tmp_path):
    calls=[]
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout('uncertain')
    engine=GoogleClient(tmp_path,httpx.MockTransport(timeout),lambda:'fake')
    with pytest.raises(ValueError,match='自動再送'):
        engine.synthesize('本文。',GoogleSettings())
    assert len(calls)==1 and engine.usage.status()['used']==3
    with pytest.raises(ValueError,match='5000バイト'):
        engine.synthesize('あ'*1700,GoogleSettings())
    assert len(calls)==1 and engine.usage.status()['used']==3
