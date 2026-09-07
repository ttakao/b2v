"""HTTP-only Style-Bert client. No TTS engine imports in the b2v environment."""
import io
import json
import wave
import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import stylebert_url



class TTSSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    model_id: str = Field(min_length=1, max_length=200)
    speaker_id: int = Field(ge=0)
    style: str = Field(min_length=1, max_length=100)
    style_weight: float = Field(default=1, ge=0, le=2)
    speed: float = Field(default=1, ge=.5, le=2)
    noise: float = Field(default=.6, ge=0, le=1)
    noise_w: float = Field(default=.8, ge=0, le=1)
    pitch_scale: float = Field(default=1, ge=.5, le=2)
    intonation_scale: float = Field(default=1, ge=0, le=2)


def inspect_wav(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as wav:
            channels, width, rate, frames = wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            if channels < 1 or width not in (1,2,3,4) or rate <= 0 or frames <= 0 or wav.getcomptype()!='NONE':
                raise ValueError('WAV形式が不正です。')
            if len(wav.readframes(frames)) != frames*channels*width:
                raise ValueError('WAVデータが途中で切れています。')
            return {'sample_rate':rate, 'channels':channels, 'sample_width':width, 'duration':frames/rate,
                    'dtype':f'PCM{width*8}', 'size_bytes':len(data)}
    except (wave.Error, EOFError) as exc:
        raise ValueError('有効なWAV音声を取得できませんでした。') from exc


class StyleBertClient:
    def __init__(self, transport=None):
        self.transport = transport

    def request(self, method, path, body=None, timeout=10):
        try:
            with httpx.Client(base_url=stylebert_url(), trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(timeout, connect=3), transport=self.transport) as client:
                response = client.request(method, path, json=body)
            if response.status_code != 200:
                try:
                    detail = response.json().get('detail')
                except ValueError:
                    detail = None
                raise ValueError(detail if isinstance(detail,str) else f'Style-Bert応答エラー ({response.status_code})')
            return response
        except httpx.TimeoutException as exc:
            raise ValueError('Style-Bertの応答がタイムアウトしました。サーバーの処理が終了してから再試行してください。') from exc
        except httpx.RequestError as exc:
            raise ValueError(f'Style-Bert-VITS2: {stylebert_url()} に接続できません。./run-stylebert.sh でサーバーを起動してください。') from exc

    def health(self):
        data = self.request('GET','/health').json()
        if data.get('engine')!='Style-Bert-VITS2' or data.get('status')!='ok':
            raise ValueError(f'{stylebert_url()} のStyle-Bert Serverを確認してください。')
        return data

    def list_models(self):
        return self.request('GET','/models', timeout=30).json()

    def synthesize(self, text, settings):
        parameters = settings.model_dump()
        parameters['length'] = 1 / parameters.pop('speed')
        response = self.request('POST','/synthesize', {'text':text, **parameters}, timeout=300)
        if 'audio/wav' not in response.headers.get('content-type',''):
            raise ValueError('Style-BertからWAV以外の応答を受信しました。')
        info = inspect_wav(response.content)
        try:
            metadata = json.loads(response.headers['X-TTS-Metadata'])
            identity = metadata['model_identity']
            if identity['model_id'] != settings.model_id or not identity['weight_sha256']:
                raise ValueError('モデル識別情報が不正です。')
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('Style-Bertのモデル識別情報がありません。') from exc
        return response.content, {**metadata, **info}
