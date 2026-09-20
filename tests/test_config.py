from urllib.parse import urlsplit
from fastapi.testclient import TestClient
from b2v.config import DEFAULTS, data_dir, service_url
from b2v.api import create_app, NarrationRequest
from b2v.narration import LLMSettings
from b2v.launch import command


def test_defaults(monkeypatch):
    for key in DEFAULTS:monkeypatch.delenv(key,raising=False)
    assert str(data_dir())=='/Volumes/RAID1-6TB/b2v-data'
    assert service_url('api')=='http://127.0.0.1:8600'
    assert LLMSettings().endpoint=='http://127.0.0.1:8602'


def test_environment_end_to_end(tmp_path,monkeypatch):
    monkeypatch.setenv('B2V_DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('B2V_WORK_DIR',str(tmp_path/'work'))
    monkeypatch.setenv('B2V_API_URL','http://127.0.0.1:8690')
    monkeypatch.setenv('B2V_LLM_URL','http://127.0.0.1:8692')
    assert command('api')[-1]=='8690'
    assert NarrationRequest().endpoint=='http://127.0.0.1:8692'
    assert LLMSettings().validate().endpoint=='http://127.0.0.1:8692'
    with TestClient(create_app()) as client:
        assert client.get('/api/documents/storage').json()['root']==str(tmp_path/'data')
        assert client.get('/api/llm-defaults').json()['endpoint']=='http://127.0.0.1:8692'
        assert (tmp_path/'data/catalog.sqlite3').is_file()


def test_explicit_test_directory_overrides_env(tmp_path,monkeypatch):
    monkeypatch.setenv('B2V_DATA_DIR',str(tmp_path/'unused'))
    with TestClient(create_app(tmp_path/'test')) as client:
        assert client.get('/api/documents/storage').json()['root']==str(tmp_path/'test/catalog')
    assert not (tmp_path/'unused').exists()
