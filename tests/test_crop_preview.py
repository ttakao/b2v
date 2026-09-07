import base64
import io
import json
import numpy as np
import cv2
import pymupdf
import pytest
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.ocr import OCRSettings, crop_image, render_page


def pdf_bytes():
    doc=pymupdf.open()
    for i in range(3):
        page=doc.new_page(width=400,height=600)
        page.insert_text((40,50),f'HEADER {i+1}')
        page.insert_text((60,150),'BODY TEXT')
        page.insert_text((180,570),str(i+1))
    return doc.tobytes()


def test_preview_no_ocr_and_page_selection(tmp_path,monkeypatch):
    monkeypatch.setattr('b2v.ocr.TesseractOCR._recognize',lambda *a:pytest.fail('Preview must not invoke OCR'))
    with TestClient(create_app(tmp_path)) as client:
        uploaded=client.post('/api/previews',files={'file':('book.pdf',pdf_bytes(),'application/pdf')})
        assert uploaded.status_code==201
        token=uploaded.json()['id']
        for page in (1,2,3):
            result=client.get(f'/api/previews/{token}/pages/{page}')
            assert result.status_code==200
            data=result.json()
            image=cv2.imdecode(np.frombuffer(base64.b64decode(data['image'].split(',')[1]),np.uint8),cv2.IMREAD_GRAYSCALE)
            assert image.shape==(data['height'],data['width'])
            assert data['page']==page and data['pages']==3
            assert max(image.shape)<=1601
        assert client.get(f'/api/previews/{token}/pages/4').status_code==400
        assert client.get('/api/previews/bad/pages/1').status_code==404
        assert client.get('/api/jobs').json()==[]
        assert client.delete('/api/previews/'+token).status_code==200
        assert client.get(f'/api/previews/{token}/pages/1').status_code==404
        assert client.post('/api/previews',files={'file':('bad.pdf',b'not pdf')}).status_code==400


@pytest.mark.parametrize('crop',[{'top':-1},{'bottom':50},{'left':float('nan')},{'right':31}])
def test_invalid_crop(crop):
    with pytest.raises(ValueError):OCRSettings(**crop).validate()


def test_preview_and_ocr_use_same_fraction_at_different_resolutions():
    settings=OCRSettings(top=5,bottom=8,left=4,right=6)
    with pymupdf.open(stream=pdf_bytes(),filetype='pdf') as doc:
        for dpi in (100,300):
            full=render_page(doc[0],OCRSettings(dpi=dpi,top=0,bottom=0,left=0,right=0))
            h,w=full.shape
            actual=render_page(doc[0],OCRSettings(dpi=dpi,top=5,bottom=8,left=4,right=6))
            expected=full[round(h*.05):round(h*.92),round(w*.04):round(w*.94)]
            assert np.array_equal(actual,expected)
            assert abs(actual.shape[0]/h-.87)<2/h
            assert abs(actual.shape[1]/w-.90)<2/w


def test_confirmed_settings_saved_and_passed_to_ocr(tmp_path,monkeypatch):
    seen=[]
    def process(job,directory,save):
        seen.append(job['ocr_settings'])
    monkeypatch.setattr('b2v.application.process_ocr',process)
    with TestClient(create_app(tmp_path)) as client:
        response=client.post('/api/jobs',files={'files':('book.pdf',pdf_bytes(),'application/pdf')},data={'top':5,'bottom':8,'left':4,'right':6})
        assert response.status_code==202
        job_id=response.json()['id']
    saved=json.loads((tmp_path/job_id/'metadata.json').read_text())
    for key,value in {'top':5,'bottom':8,'left':4,'right':6}.items():
        assert saved['ocr_settings'][key]==value==seen[0][key]
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f'/api/jobs/{job_id}/crop-preview/1/2').status_code==200
        assert client.get(f'/api/jobs/{job_id}/crop-preview/2/1').status_code==404
