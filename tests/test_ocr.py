import io
import json
import time
import zipfile
import numpy as np
import pymupdf
import pytest
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.ocr import OCRSettings, crop_image, detect_direction, effective_direction, TesseractOCR, render_page
from b2v.ocr_workflow import process_ocr


def synthetic_pdf(pages=2, small=False):
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=30 if small else 300, height=40 if small else 400)
        if not small:
            if index == 0:
                for row in range(10):
                    page.insert_text((25, 50 + row * 28), '今日は良い天気です。', fontname='japan', fontsize=16)
            else:
                for col in range(8):
                    for row, char in enumerate('今日は良い天気です。'):
                        page.insert_text((260-col*30, 45+row*18), char, fontname='japan', fontsize=16)
    return doc.tobytes()


def wait_job(client, job_id):
    for _ in range(600):
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] not in ('queued', 'running'):
            return job
        time.sleep(.05)
    pytest.fail('OCR timeout')


def test_crop_direction_and_fallback():
    settings = OCRSettings(top=10, bottom=20, left=5, right=15)
    image = np.full((400, 300), 255, dtype=np.uint8)
    assert crop_image(image, settings).shape == (280, 240)
    assert detect_direction(image)['direction'] == 'unknown'
    # Dense continuous rows/columns with regular inter-line whitespace.
    for row in range(30, 370, 25):
        image[row:row+12, 20:280] = 0
    horizontal = detect_direction(image)
    assert horizontal['direction'] == 'horizontal'
    assert detect_direction(image.T.copy())['direction'] == 'vertical'
    assert effective_direction({'direction':'horizontal','confidence':.4}, 'vertical', .75) == 'vertical'
    assert effective_direction(horizontal, 'vertical', .75) == 'horizontal'
    assert effective_direction({'direction':'unknown','confidence':0}, 'auto', .75) == 'vertical'
    with pytest.raises(ValueError):
        OCRSettings(top=float('nan')).validate()


def test_resume_and_300_pages(tmp_path):
    source = tmp_path / 'source.pdf'
    source.write_bytes(synthetic_pdf(300, small=True))
    job = {'direction':'vertical','ocr_settings':OCRSettings(dpi=100).to_dict(),
           'books':[{'source':'source.pdf','name':'300ページ.pdf','pages':None,'status':'queued'}],
           'artifacts':[], 'completed':0}
    class Provider:
        calls = 0
        fail = True
        def recognize(self, image, direction, dpi):
            self.calls += 1
            if self.fail and self.calls == 3:
                raise RuntimeError('injected page failure')
            return '原文……\n'
    provider = Provider()
    with pytest.raises(RuntimeError):
        process_ocr(job, tmp_path, lambda job: None, provider)
    assert job['position']['page'] == 3
    first = tmp_path / 'ocr/001/page_0001.txt'
    timestamp = first.stat().st_mtime_ns
    provider.fail = False
    process_ocr(job, tmp_path, lambda job: None, provider)
    assert provider.calls == 301  # 300 successes + one failed attempt; first two skipped
    assert first.stat().st_mtime_ns == timestamp
    assert job['ocr_completed'] == 300
    assert len(list((tmp_path/'ocr/001').glob('*.txt'))) == 300
    assert (tmp_path/'results/001_raw_ocr.txt').read_text().count('原文……') == 300


@pytest.mark.parametrize('direction,page_index', [('horizontal', 0), ('vertical', 1)])
def test_real_tesseract_upload(tmp_path, direction, page_index):
    # Integration test requires the installed Japanese language packs.
    assert TesseractOCR().check()['ready']
    with TestClient(create_app(tmp_path)) as client:
        document = pymupdf.open(stream=synthetic_pdf(), filetype='pdf')
        document.select([page_index])
        response = client.post('/api/jobs', files={'files':('日本語.pdf',document.tobytes())},
                               data={'direction':direction, 'threshold':1})
        assert response.status_code == 202
        job = wait_job(client, response.json()['id'])
        assert job['status'] == 'completed', job.get('error')
        assert job['ocr_completed'] == job['ocr_total'] == 1
        for page in (1,):
            result = client.get(f"/api/jobs/{job['id']}/pages/1/{page}")
            assert result.status_code == 200
            assert '天気' in ''.join(result.json()['text'].split()), result.json()
        assert client.get(f"/api/jobs/{job['id']}/pages/1/3").status_code == 404
        archive = client.get(f"/api/jobs/{job['id']}/files/results.zip")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert 'ocr/001/page_0001.txt' in z.namelist()
            assert 'ocr/001/page_0001.json' in z.namelist()
        assert client.get('/api/ocr-health').json()['ready']
        assert client.post('/api/jobs', files={'files':('test.pdf', synthetic_pdf())}, data={'top':31}).status_code == 400


def test_short_horizontal_does_not_override_as_vertical():
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    for row, text in enumerate(['今日は良い天気です。', '川沿いの道をゆっくり歩く。', '静かな午後の物語が始まる。']):
        page.insert_text((25, 65 + row * 40), text, fontname='japan', fontsize=16)
    result = detect_direction(render_page(page, OCRSettings()))
    assert effective_direction(result, 'horizontal', .75) == 'horizontal', result
