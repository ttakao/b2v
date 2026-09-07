"""Environment configuration shared by the application and service launchers."""
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    'B2V_DATA_DIR': '/Volumes/RAID1-6TB/b2v-data',
    'B2V_API_URL': 'http://127.0.0.1:8600',
    'B2V_LLM_URL': 'http://127.0.0.1:8602',
    'B2V_STYLEBERT_URL': 'http://127.0.0.1:8603',
}


def data_dir():
    return Path(os.environ.get('B2V_DATA_DIR', DEFAULTS['B2V_DATA_DIR'])).expanduser().resolve()


def service_url(service):
    key = 'B2V_'+service.upper()+'_URL'
    value = os.environ.get(key, DEFAULTS[key]).rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError(f'{key}にはパスを含まないHTTP(S) URLを指定してください。')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError(f'{key}のポートが不正です。')
    return value


def llm_url():return service_url('llm')
def stylebert_url():return service_url('stylebert')
