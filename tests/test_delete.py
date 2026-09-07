from fastapi.testclient import TestClient
import pytest
from b2v.api import create_app


def seed(app, root, job_id, state, parent=None):
    folder=root/job_id
    folder.mkdir()
    (folder/'source.pdf').write_bytes(b'original')
    (folder/'result.txt').write_text('result')
    job={'id':job_id,'created_at':'2026-01-01','status':state,'books':[]}
    if parent:job['ocr_parent']=parent
    app.state.jobs.save(job)


@pytest.mark.parametrize('state',['completed','failed','interrupted'])
def test_delete_removes_only_selected_job(tmp_path,state):
    app=create_app(tmp_path)
    with TestClient(app) as client:
        seed(app,tmp_path,'a'*32,state)
        seed(app,tmp_path,'b'*32,'completed')
        response=client.delete('/api/jobs/'+'a'*32)
        assert response.status_code==200
        assert not (tmp_path/('a'*32)).exists()
        assert (tmp_path/('b'*32)/'result.txt').read_text()=='result'
        assert client.get('/api/jobs/'+'a'*32).status_code==404
        assert len(client.get('/api/jobs').json())==1


@pytest.mark.parametrize('state',['queued','running'])
def test_active_job_cannot_be_deleted(tmp_path,state):
    app=create_app(tmp_path)
    with TestClient(app) as client:
        seed(app,tmp_path,'a'*32,state)
        assert client.delete('/api/jobs/'+'a'*32).status_code==409
        assert (tmp_path/('a'*32)/'source.pdf').exists()


def test_parent_must_outlive_child(tmp_path):
    app=create_app(tmp_path)
    with TestClient(app) as client:
        seed(app,tmp_path,'a'*32,'completed')
        seed(app,tmp_path,'b'*32,'failed',parent='a'*32)
        assert client.delete('/api/jobs/'+'a'*32).status_code==409
        assert client.delete('/api/jobs/'+'b'*32).status_code==200
        assert (tmp_path/('a'*32)/'source.pdf').exists()
        assert client.delete('/api/jobs/'+'a'*32).status_code==200
        assert client.delete('/api/jobs/unknown').status_code==404
