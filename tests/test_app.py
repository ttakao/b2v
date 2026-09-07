import io
import json
import time
import zipfile
from pypdf import PdfWriter
from fastapi.testclient import TestClient
from b2v.api import create_app


def pdf():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.write(output)
    return output.getvalue()


def finish(client, job_id):
    for _ in range(200):
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(.01)
    raise AssertionError('Job did not finish')


def test_upload_download_persistence(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/').status_code == 200
        response = client.post('/api/jobs', files=[('files', ('../日本語.pdf', pdf(), 'application/pdf')),
                                                    ('files', ('日本語.pdf', pdf(), 'application/pdf'))],
                               data={'direction': 'horizontal', 'mode': 'receipt'})
        assert response.status_code == 202
        job = finish(client, response.json()['id'])
        assert job['status'] == 'completed'
        assert job['completed'] == 2
        assert job['books'][0]['name'] == '日本語.pdf'
        assert job['books'][0]['pages'] == 1
        assert job['direction'] == 'horizontal'
        base = f"/api/jobs/{job['id']}/files/"
        report = client.get(base + job['artifacts'][0]['path'])
        assert '未実装' in report.text
        archive = zipfile.ZipFile(io.BytesIO(client.get(base + 'results.zip').content))
        assert len(archive.namelist()) == 2
        assert client.get(base + 'metadata.json').status_code == 404
        assert client.get(base + 'source/001.pdf').status_code == 404
        assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 409
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/jobs').json()[0]['status'] == 'completed'


def test_failure_preserves_success(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/jobs', files=[('files', ('ok.pdf', pdf())), ('files', ('bad.pdf', b'not pdf'))], data={'mode': 'receipt'})
        job = finish(client, response.json()['id'])
        assert job['status'] == 'failed'
        assert job['error']['phase'] == 'receipt'
        assert len(job['artifacts']) == 1
        assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 202
        retried = finish(client, job['id'])
        assert retried['completed'] == 1
        assert len(retried['artifacts']) == 1
        assert client.get(f"/api/jobs/{job['id']}/files/" + job['artifacts'][0]['path']).status_code == 200


def test_validation_and_interruption(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/api/jobs', files={'files': ('bad.txt', b'abc')}).status_code == 400
        assert not [p for p in tmp_path.iterdir() if p.name != 'catalog']
        assert client.post('/api/jobs', files={'files': ('ok.pdf', pdf())}, data={'direction':'invalid'}).status_code == 422
        assert client.get('/api/jobs/unknown').status_code == 404
        response = client.post('/api/jobs', files={'files': ('ok.pdf', pdf())}, data={'mode': 'receipt'})
        job = finish(client, response.json()['id'])
    path = tmp_path / job['id'] / 'metadata.json'
    job['status'] = 'running'
    path.write_text(json.dumps(job))
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/api/jobs/{job['id']}").json()['status'] == 'interrupted'
        assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 202
        assert finish(client, job['id'])['status'] == 'completed'
