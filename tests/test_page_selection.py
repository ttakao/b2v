import json
import time
import io
import zipfile
import numpy as np
import pytest
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.ocr import OCRSettings, horizontal_blocks, detect_direction, render_page
from b2v.page_selection import parse_pages, selected_pages
from b2v.narration_workflow import process_narration
from test_ocr import synthetic_pdf


def test_page_selection_validation():
    assert parse_pages('2-4,8,3', 9) == [2,3,4,8]
    assert selected_pages(OCRSettings(pages='2-8',exclude_pages='3,5-7'),9) == [2,4,8]
    assert parse_pages('',3) == [1,2,3]
    for value in ['0','3-2','5','1,,2','abc']:
        with pytest.raises(ValueError):parse_pages(value,4)
    with pytest.raises(ValueError):OCRSettings(horizontal_pages='2',vertical_pages='2-3').validate()
    with pytest.raises(ValueError):selected_pages(OCRSettings(exclude_pages='1-3'),3)


def test_selected_ocr_pages_and_forced_direction(tmp_path, monkeypatch):
    calls=[]
    class Provider:
        def check(self):return {'ready':True}
        def recognize(self,image,direction,dpi):calls.append(direction);return '縦書き結果'
        def recognize_blocks(self,image,dpi):calls.append('blocks');return '横書き結果', [[0,0,10,10]]
    monkeypatch.setattr('b2v.ocr_workflow.TesseractOCR',Provider)
    with TestClient(create_app(tmp_path)) as client:
        response=client.post('/api/jobs',files={'files':('sample.pdf',synthetic_pdf(4))},
                    data={'pages':'2-4','exclude_pages':'3','horizontal_pages':'2','vertical_pages':'4'})
        job_id=response.json()['id']
        for _ in range(100):
            job=client.get(f'/api/jobs/{job_id}').json()
            if job['status'] not in ('running','queued'):break
            time.sleep(.01)
        assert job['status']=='completed',job
        assert job['ocr_total']==job['ocr_completed']==2
        assert job['books'][0]['selected_pages']==[2,4]
        assert calls==['blocks','vertical']
        root=tmp_path/job_id
        assert not (root/'ocr/001/page_0001.txt').exists()
        assert not (root/'ocr/001/page_0003.txt').exists()
        assert client.get(f'/api/jobs/{job_id}/pages/1/3').status_code==404
        assert client.get(f'/api/jobs/{job_id}/pages/1/2').json()['direction_source']=='manual'
        archive=client.get(f'/api/jobs/{job_id}/files/results.zip')
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert 'ocr/001/page_0003.txt' not in z.namelist()
        before=(root/'metadata.json').read_bytes()
        new=client.post(f'/api/jobs/{job_id}/reprocess-ocr',json={'direction':'vertical','settings':{'pages':'4','vertical_pages':'4'}})
        assert new.status_code==202
        assert new.json()['id']!=job_id
        assert (root/'metadata.json').read_bytes()==before


def test_narration_does_not_read_excluded_pages(tmp_path):
    folder=tmp_path/'raw/ocr/001';folder.mkdir(parents=True)
    (folder/'page_0002.txt').write_text('本文二。')
    (folder/'page_0004.txt').write_text('本文四。')
    job={'books':[{'name':'sample','pages':4,'selected_pages':[2,4]}], 'llm_settings':__import__('b2v.narration',fromlist=['LLMSettings']).LLMSettings().to_dict(),'artifacts':[]}
    seen=[]
    class Provider:
        def request_edits(self,source,previous,following):seen.append((source,previous,following));return {'edits':[]}
    process_narration(job,tmp_path/'output',tmp_path/'raw',lambda job:None,Provider())
    assert seen==[('本文二。','',''),('本文四。','','')]
    assert job['llm_completed']==2
    assert not (tmp_path/'output/narration/001/page_0003.txt').exists()


def test_horizontal_line_regions_do_not_overlap():
    image=np.full((200,500),255,dtype=np.uint8)
    for y in [30,90,150]:
        for x in range(30,450,35):
            image[y:y+20,x:x+20]=0
            image[y+3:y+6,x+5:x+12]=255
    boxes=horizontal_blocks(image)
    assert len(boxes)==3
    assert all(a[1]+a[3]<=b[1] for a,b in zip(boxes,boxes[1:]))
