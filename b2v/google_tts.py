"""Google Neural2 via ADC and REST; durable, conservative usage accounting."""
import base64
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field
from .audio_format import inspect_wav


class GoogleSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    engine: Literal['google'] = 'google'
    model_id: Literal['ja-JP-Neural2-C', 'ja-JP-Neural2-D'] = 'ja-JP-Neural2-C'
    speaker_id: Literal[0] = 0
    style: Literal['Neutral'] = 'Neutral'
    speed: float = Field(default=1, ge=.5, le=2)
    pitch: float = Field(default=0, ge=-20, le=20)


def project():
    return os.environ.get('B2V_GOOGLE_PROJECT', 'text2voice-509213')


def monthly_limit():
    value = int(os.environ.get('B2V_GOOGLE_MONTHLY_LIMIT', '900000'))
    if not 0 <= value <= 1000000:
        raise ValueError('B2V_GOOGLE_MONTHLY_LIMITは0〜1000000文字で指定してください。')
    return value


def billing_month():
    return datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m')


class Usage:
    """Reserve before sending; even uncertain/failed requests keep their charge.

    Independent of books/chunks, so deleting an MP3 never resets the counter.
    BEGIN IMMEDIATE also serializes reservations across local processes.
    """
    def __init__(self, root):
        self.path = Path(root) / 'google_tts_usage.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS usage (project TEXT, month TEXT, characters INTEGER NOT NULL, PRIMARY KEY(project,month))')
            db.execute('CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def status(self):
        month = billing_month()
        with self.connect() as db:
            row = db.execute('SELECT characters FROM usage WHERE project=? AND month=?', (project(), month)).fetchone()
        used = row[0] if row else 0
        limit = monthly_limit()
        return {'project': project(), 'month': month, 'used': used, 'limit': limit, 'remaining': max(0, limit-used), 'timezone': 'America/Los_Angeles'}

    def reserve(self, characters):
        if characters <= 0:
            raise ValueError('送信文字数が不正です。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            month = billing_month()
            row = db.execute('SELECT characters FROM usage WHERE project=? AND month=?', (project(), month)).fetchone()
            used = row[0] if row else 0
            if used + characters > monthly_limit():
                raise ValueError('Google音声の月間上限に達するため停止しました。成功済み音声は保存されています。')
            db.execute('INSERT INTO usage VALUES (?,?,?) ON CONFLICT(project,month) DO UPDATE SET characters=excluded.characters', (project(), month, used+characters))

    def record_external(self, key, month, characters):
        """Idempotently include known standalone tests or other external use."""
        if characters < 0:
            raise ValueError('文字数は0以上にしてください。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM imports WHERE id=?', (key,)).fetchone():
                return
            db.execute('INSERT INTO usage VALUES (?,?,?) ON CONFLICT(project,month) DO UPDATE SET characters=characters+excluded.characters', (project(), month, characters))
            db.execute('INSERT INTO imports VALUES (?)', (key,))


_token_lock = threading.Lock()
_token = None
_token_until = 0


def access_token():
    global _token, _token_until
    with _token_lock:
        if _token and time.monotonic() < _token_until:
            return _token
        executable = shutil.which('gcloud')
        if not executable and Path('/opt/homebrew/bin/gcloud').is_file():
            executable = '/opt/homebrew/bin/gcloud'
        if not executable:
            raise ValueError('Google Cloud CLIがありません。brew install --cask gcloud-cli を実行してください。')
        try:
            result = subprocess.run([executable, 'auth', 'application-default', 'print-access-token'], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Google認証がタイムアウトしました。接続を確認してください。') from exc
        if result.returncode or not result.stdout.strip():
            raise ValueError('GoogleのADC認証を取得できません。ターミナルで gcloud auth application-default login を実行してください。')
        _token = result.stdout.strip()
        _token_until = time.monotonic() + 2700
        return _token


def model_identity(name):
    # Google does not expose a weight/version hash. This identifies our request
    # contract, not immutable remote weights. Preserve old chunks on resume.
    return {'engine': 'google-neural2', 'model_id': name, 'api': 'v1', 'adapter': 1, 'sample_rate': 24000}


class GoogleClient:
    def __init__(self, root, transport=None, token_provider=access_token):
        self.usage = Usage(root)
        self.transport = transport
        self.token_provider = token_provider

    def request(self, method, path, body=None, characters=0):
        token = self.token_provider()
        with httpx.Client(base_url='https://texttospeech.googleapis.com/v1',
                          headers={'Authorization': 'Bearer '+token, 'x-goog-user-project': project()},
                          timeout=httpx.Timeout(120, connect=10), follow_redirects=False,
                          trust_env=False, transport=self.transport) as client:
            if characters:
                self.usage.reserve(characters)
            try:
                response = client.request(method, path, json=body)
            except httpx.HTTPError as exc:
                raise ValueError('Google音声との通信に失敗しました。自動再送はしません。送信予定分は使用量に計上済みです。接続を確認して再開してください。') from exc
        if response.status_code != 200:
            messages = {401: 'ADC認証を更新してください。', 403: 'APIの有効化・請求先・プロジェクトの権限を確認してください。', 429: '利用制限中です。しばらく待って再開してください。'}
            raise ValueError(f'Google音声エラー ({response.status_code})。'+messages.get(response.status_code, '設定とGoogle Cloudの稼働状況を確認してください。'))
        return response.json()

    def health(self):
        names = {v['name'] for v in self.request('GET', '/voices?languageCode=ja-JP').get('voices', [])}
        if not {'ja-JP-Neural2-C', 'ja-JP-Neural2-D'} <= names:
            raise ValueError('Google Neural2の日本語音声を確認できません。')
        return {'status': 'ok', 'engine': 'Google Neural2', 'project': project()}

    def list_models(self):
        return {'models': [{'id': name, 'name': name+'（男性）', 'speakers': [{'id': 0, 'name': '男性'}], 'styles': ['Neutral'], 'identity': model_identity(name)} for name in ('ja-JP-Neural2-C', 'ja-JP-Neural2-D')],
                'defaults': GoogleSettings().model_dump(), 'max_text_chars': 300}

    def synthesize(self, text, settings):
        if not text.strip() or len(text.encode('utf-8')) > 5000:
            raise ValueError('Google音声の本文は空白以外を含む5000バイト以内で指定してください。')
        started = time.monotonic()
        result = self.request('POST', '/text:synthesize', {
            'input': {'text': text},
            'voice': {'languageCode': 'ja-JP', 'name': settings.model_id},
            'audioConfig': {'audioEncoding': 'LINEAR16', 'sampleRateHertz': 24000, 'speakingRate': settings.speed, 'pitch': settings.pitch},
        }, characters=len(text))
        try:
            data = base64.b64decode(result['audioContent'], validate=True)
            info = inspect_wav(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Googleから有効なWAVを取得できませんでした。送信分は使用量に計上済みです。') from exc
        if (info['channels'], info['sample_width'], info['sample_rate']) != (1, 2, 24000):
            raise ValueError('Google音声の形式がmono PCM16 / 24000Hzではありません。')
        return data, {**info, 'model_identity': model_identity(settings.model_id), 'tts_seconds': time.monotonic()-started}
