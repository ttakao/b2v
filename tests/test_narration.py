import json
import time
import io
import zipfile
import httpx
import pytest
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.narration import LLMSettings, LlamaNarrator, split_text, inspect_change
from b2v.narration_workflow import process_narration


def test_chunking_preserves_every_character():
    source = '彼は言った。「待って！」\n\n雨が降る。' * 40 + 'あ' * 501 + '\n'
    chunks = split_text(source, 200)
    assert ''.join(chunks) == source
    assert max(map(len, chunks)) <= 200
    assert split_text('一文。\n\n次の文。', 6) == ['一文。\n\n', '次の文。']
    assert split_text('', 200) == []


@pytest.mark.parametrize('endpoint', ['https://example.com', 'http://localhost:8602',
    'http://127.0.0.1:8500', 'http://127.0.0.1:8602@evil.com', 'http://127.0.0.1:8602/redirect'])
def test_external_endpoint_rejected(endpoint):
    with pytest.raises(ValueError):
        LLMSettings(endpoint=endpoint).validate()


def test_change_warnings():
    assert not inspect_change('彼は たたずんだーーー', '彼はたたずんだ……')['warnings']
    assert inspect_change('彼はたたずんだーーー', '彼はたたずんだ')['warnings']
    assert len(inspect_change('彼はたたずんだ。雨が降っていた。', '彼は立った。')['warnings']) == 2


def fixture_job(tmp_path):
    raw = tmp_path/'raw/ocr/001';raw.mkdir(parents=True)
    (raw/'page_0001.txt').write_text('雨が降る。'*45)
    (raw/'page_0002.txt').write_text('そして彼はたたずんだーーー')
    job = {'books':[{'pages':2, 'name':'本'}], 'llm_settings':LLMSettings(chunk_chars=200).to_dict(),
           'artifacts':[]}
    return job


def test_narration_resume_no_ocr_and_context(tmp_path):
    job = fixture_job(tmp_path)
    class Provider:
        calls = 0
        failing = True
        seen = []
        def request_edits(self, source, previous, following):
            self.calls += 1
            self.seen.append((source, previous, following))
            if self.failing and self.calls == 2:
                raise RuntimeError('failure')
            return {'edits':[{'original':'ーーー','replacement':'……','before_context':'','after_context':'','reason':'余韻記号変換'}] if 'ーーー' in source else []}
    provider = Provider()
    output = tmp_path/'output'
    with pytest.raises(RuntimeError):
        process_narration(job, output, tmp_path/'raw', lambda job: None, provider)
    checkpoint=output/'narration/001/page_0001_chunk_0001.json'
    timestamp=checkpoint.stat().st_mtime_ns
    provider.failing=False
    process_narration(job, output, tmp_path/'raw', lambda job: None, provider)
    assert provider.calls == 4
    assert timestamp == checkpoint.stat().st_mtime_ns
    assert job['llm_completed'] == 2
    assert job['books'][0]['narration_completed'] == 2
    assert job['review_count'] == 0
    assert provider.seen[-1][1] == ('雨が降る。'*45)[-150:]
    assert json.loads((output/'narration/001/page_0002.json').read_text())['source'].endswith('ーーー')
    assert '……' in (output/'results/001_narration.txt').read_text()


def test_api_narration_from_existing_ocr(tmp_path, monkeypatch):
    class Provider:
        def __init__(self, settings): pass
        def health(self): return {'ready':True}
        def request_edits(self, source, previous, following): return {'edits':[{'original':'ーーー','replacement':'……','before_context':'','after_context':'','reason':'余韻記号変換'}] if 'ーーー' in source else []}
    monkeypatch.setattr('b2v.narration_workflow.LlamaNarrator', Provider)
    parent_id='a'*32
    folder=tmp_path/parent_id/'ocr/001';folder.mkdir(parents=True)
    original=folder/'page_0001.txt';original.write_text('彼はたたずんだーーー')
    metadata={'id':parent_id, 'created_at':'2026-01-01','status':'completed','mode':'ocr',
              'books':[{'pages':1,'name':'原本.pdf'}],'total':1}
    (tmp_path/parent_id/'metadata.json').write_text(json.dumps(metadata))
    with TestClient(create_app(tmp_path)) as client:
        response=client.post(f'/api/jobs/{parent_id}/narration',json={})
        assert response.status_code == 202
        job_id=response.json()['id']
        for _ in range(100):
            job=client.get(f'/api/jobs/{job_id}').json()
            if job['status'] not in ('queued','running'):break
            time.sleep(.01)
        assert job['status'] == 'completed',job
        assert original.read_text() == '彼はたたずんだーーー'
        result=client.get(f'/api/jobs/{job_id}/narration/1/1').json()
        assert result['narration']=='彼はたたずんだ……'
        assert client.get(f'/api/jobs/{job_id}/narration/2/1').status_code == 404
        archive=client.get(f'/api/jobs/{job_id}/files/results.zip')
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert 'narration/001/page_0001_chunk_0001.json' in z.namelist()
        assert client.post(f'/api/jobs/{parent_id}/narration',json={'endpoint':'https://example.com'}).status_code == 400
        assert client.post(f'/api/jobs/{job_id}/narration',json={}).status_code == 400
        assert client.get(f'/api/jobs/{parent_id}').json()==metadata


def test_truncated_llm_response_rejected(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False
            assert kwargs['follow_redirects'] is False
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self, url, json):
            assert json['chat_template_kwargs']['enable_thinking'] is False
            return httpx.Response(200,request=httpx.Request('POST',url),json={'choices':[{
                'finish_reason':'length','message':{'content':'{"narration":"途中"}'}}]})
    monkeypatch.setattr('b2v.narration.httpx.Client',Client)
    with pytest.raises(RuntimeError, match='途中終了'):
        LlamaNarrator(LLMSettings()).convert('長文','','')
