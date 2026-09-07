import io
import json
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.stylebert import StyleBertClient, TTSSettings, inspect_wav


def audio():
    stream=io.BytesIO()
    with wave.open(stream,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(22050);w.writeframes(b'\x00\x00'*2205)
    return stream.getvalue()


def settings():
    return TTSSettings(model_id='voice',speaker_id=0,style='Neutral')


INFO={'model_identity':{'model_id':'voice','weight_sha256':'abcd'},'tts_seconds':.1}


def test_http_client_speed_mapping_and_wav_metadata():
    seen=[]
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,content=audio(),headers={'content-type':'audio/wav','X-TTS-Metadata':json.dumps(INFO)})
    client=StyleBertClient(httpx.MockTransport(handle))
    s=settings().model_copy(update={'speed':1.25,'style_weight':.75,'noise_w':.5})
    data,info=client.synthesize('本文',s)
    assert seen[0]['length']==.8 and 'speed' not in seen[0]
    assert seen[0]['style_weight']==.75 and seen[0]['noise_w']==.5
    assert info['sample_rate']==22050 and info['duration']==.1
    assert data==audio()


def test_http_client_offline_and_invalid_audio():
    def offline(request):raise httpx.ConnectError('offline')
    with pytest.raises(ValueError,match='接続できません'):
        StyleBertClient(httpx.MockTransport(offline)).health()
    with pytest.raises(ValueError):inspect_wav(b'not wave')
    with pytest.raises(ValueError):inspect_wav(audio()[:-2])
    with pytest.raises(ValueError):TTSSettings(**{**settings().model_dump(),'speed':0})


