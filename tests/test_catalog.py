import io
import json
import time
import pytest
import pymupdf
from fastapi.testclient import TestClient
from b2v.api import create_app
from b2v.catalog import Catalog
from b2v.quality import parse_tsv, statistics, classify
from b2v.ocr import OCRSettings
from b2v.narration import LLMSettings


def pdf():
    doc=pymupdf.open()
    for _ in range(4):doc.new_page(width=200,height=300).insert_text((20,40),'A book page')
    return doc.tobytes()


class OCR:
    calls=0
    def check(self):pass
    def recognize_with_confidence(self,*args):
        self.calls+=1
        return {'text':f'第 {self.calls} ページ。誰もいなかつた。', 'units':[{'text':'誰も','confidence':20 if self.calls in (1,2) else 95}]*25 if self.calls!=4 else [],'tsvs':['level\tconf\ttext\n5\t20\t誰も\n'],'blocks':[]}


@pytest.fixture
def catalog(tmp_path):
    c=Catalog(tmp_path);d=c.add('本.pdf',io.BytesIO(pdf()));c.ocr(d['id'],OCRSettings(), 'vertical',OCR())
    yield c,d['id']
    c.executor.shutdown(wait=True)


def test_tsv_invalid_and_statistics():
    rows=parse_tsv('level\tconf\ttext\n5\t10\t低\n5\t40\t中\n5\t100\t高\n4\t-1\t\n5\tnan\t無\n5\t101\t無\n5\t50\t \n')
    result=statistics(rows)
    assert result['recognized_unit_count']==3
    assert result['mean_ocr_confidence']==50
    assert result['low_confidence_count']==2 and result['very_low_confidence_count']==1
    assert result['low_confidence_ratio']==2/3
    assert statistics([])['mean_ocr_confidence'] is None
    assert statistics([])['low_confidence_ratio'] is None


def test_ranking_ties_and_no_confidence(catalog):
    c,ident=catalog;pages=c.pages(ident)
    assert [p['quality'] for p in pages]==['REVIEW','REVIEW','AUTO_OK','UNAVAILABLE']
    assert [p['llm_selected'] for p in pages]==[True,True,False,False]
    assert pages[3]['candidate']
    c.rank(ident,{'percentile':100,'minimum':20,'low':50,'very_low':20})
    assert all(p['candidate'] for p in c.pages(ident))
    assert not c.page(ident,4)['llm_selected']


def test_low_text_separate():
    pages=[dict(page_number=1,**statistics([{'text':'名','confidence':0}])),dict(page_number=2,**statistics([{'text':'字','confidence':90}]*25))]
    classify(pages)
    assert pages[0]['quality']=='LOW_TEXT' and not pages[0]['auto_selected']
    assert pages[1]['quality']=='REVIEW'


def test_recalculation_preserves_human_and_manual_choices(catalog):
    c,ident=catalog;c.edit(ident,1,{'text':'人間の文章','selection_override':False})
    c.edit(ident,3,{'selection_override':True})
    c.rank(ident,{'percentile':50,'minimum':20,'low':30,'very_low':10})
    assert c.text(ident,1)=='人間の文章' and c.page(ident,1)['human_checked']
    assert not c.page(ident,1)['llm_selected'] and c.page(ident,3)['llm_selected']


def test_exclude_restore_keeps_text(catalog):
    c,ident=catalog;c.edit(ident,1,{'text':'残す本文'})
    c.edit(ident,1,{'included':False});c.rank(ident)
    assert not c.page(ident,1)['llm_selected']
    assert '残す本文' not in c.final(ident).read_text()
    c.edit(ident,1,{'included':True})
    assert '残す本文' in c.final(ident).read_text()
    assert c.page(ident,1)['human_checked']


class LLM:
    def __init__(self):self.seen=[]
    def health(self):pass
    def request_edits(self,current,previous,following):
        self.seen.append((current,previous,following))
        self.metrics_sink({'current_chars':len(current),'completion_tokens':10})
        return {'edits':[dict(original='いなかつた',replacement='いなかった',before_context='',after_context='',reason='OCR誤認識')] if 'いなかつた' in current else []}


def test_selected_llm_uses_current_human_text_and_real_neighbors(catalog):
    c,ident=catalog;c.edit(ident,1,{'text':'人が直した文。誰もいなかつた。'})
    old2=c.text(ident,2);texts={p['page_number']:c.text(ident,p['page_number']) for p in c.pages(ident)}
    provider=LLM();c.llm(ident,LLMSettings(),[1,3],texts,provider)
    assert provider.seen[0][0].startswith('人が直した文。')
    assert provider.seen[0][2]==old2
    assert provider.seen[1][1]==old2
    assert c.text(ident,2)==old2
    assert c.text(ident,1)=='人が直した文。誰もいなかった。'
    assert not c.page(ident,1)['human_checked'] and c.page(ident,1)['candidate']
    assert c.page(ident,1)['llm_status']=='completed'
    again=LLM();texts[1]=c.text(ident,1);c.llm(ident,LLMSettings(),[1],texts,again)
    assert again.seen[0][0]==c.text(ident,1)


def test_failed_llm_retains_current_text(catalog):
    c,ident=catalog;source=c.text(ident,1)
    class Fail(LLM):
        def request_edits(self,*args):raise RuntimeError('test failure')
    c.llm(ident,LLMSettings(),[1],{1:source},Fail())
    assert c.text(ident,1)==source
    assert c.page(ident,1)['llm_status']=='failed' and c.page(ident,1)['candidate']


def test_deletions_independent_and_reopen(catalog):
    c,ident=catalog;c.edit(ident,1,{'text':'校正済み'})
    c.remove(ident,'pdf')
    assert c.text(ident,1)=='校正済み' and (c.folder(ident)/'ocr/page_0001.txt').exists()
    c.remove(ident,'ocr')
    assert c.text(ident,1)=='校正済み'
    assert c.page(ident,1)['quality']=='UNAVAILABLE'
    reopened=Catalog(c.root)
    try:assert reopened.page(ident,1)['human_checked'] and reopened.text(ident,1)=='校正済み'
    finally:reopened.executor.shutdown()
    c.remove(ident,'text')
    with pytest.raises(FileNotFoundError):c.doc(ident)
    assert not c.folder(ident).exists()
    with c.connect() as db:assert db.execute('SELECT count(*) FROM pages WHERE document_id=?',(ident,)).fetchone()[0]==0


def test_ocr_overwrite_preserves_text_until_explicit_regeneration(catalog):
    c,ident=catalog;c.edit(ident,1,{'text':'人間編集'})
    c.ocr(ident,OCRSettings(),'vertical',OCR())
    assert c.text(ident,1)=='人間編集' and c.page(ident,1)['text_stale']
    c.regenerate(ident)
    assert c.text(ident,1).startswith('第 1 ページ。')
    assert not c.page(ident,1)['human_checked']


def test_empty_human_text_and_revision_guard(catalog):
    c,ident=catalog;revision=c.page(ident,1)['text_revision']
    c.edit(ident,1,{'text':'','expected_revision':revision})
    assert c.text(ident,1)==''
    with pytest.raises(ValueError):c.edit(ident,1,{'text':'古い入力','expected_revision':revision})


def test_api_human_flow_and_asset_deletion(tmp_path,monkeypatch):
    monkeypatch.setattr('b2v.catalog.ConfidenceOCR',OCR)
    with TestClient(create_app(tmp_path)) as client:
        added=client.post('/api/documents',files={'file':('本.pdf',pdf(),'application/pdf')});assert added.status_code==201
        ident=added.json()['id'];base='/api/documents/'+ident
        assert client.post(base+'/ocr',json={'settings':OCRSettings().to_dict(),'direction':'vertical'}).status_code==202
        for _ in range(200):
            if not client.get(base).json()['busy']:break
            time.sleep(.01)
        assert client.get(base).json()['error'] is None
        assert len(client.get(base+'/pages').json())==4
        assert client.patch(base+'/pages/1',json={'text':'本文変更'}).status_code==200
        assert client.get(base+'/pages/1').json()['text']=='本文変更'
        assert not client.get(base).json()['final_exists']
        assert client.post(base+'/final').status_code==200
        assert client.get(base).json()['final_exists']
        assert client.get(base).json()['final_current']
        assert '本文変更' in client.get(base+'/final').text
        assert client.post(base+'/audio-decision',json={'status':'approved'}).status_code==200
        assert client.patch(base+'/pages/1',json={'included':False}).status_code==200
        assert not client.get(base).json()['final_current']
        assert client.get(base).json()['final_exists']
        assert client.get('/api/documents').json()[0]['final_exists']
        assert client.get(base).json()['audio_conversion_status']=='pending'
        assert client.get(base+'/review-image/1').status_code==200
        assert client.delete(base+'/assets/pdf').status_code==200
        assert client.get(base+'/pages/1').json()['text']=='本文変更'
        assert client.get(base+'/review-image/1').status_code==400
        assert client.put(base+'/quality',json={'percentile':101}).status_code==422


def test_tsv_literal_quotes_do_not_swallow_following_rows():
    units=parse_tsv('level\tconf\ttext\n5\t20\t"引用\n5\t80\t次\n')
    assert len(units)==2 and units[0]['text']=='"引用'


def test_start_llm_selection_human_rerun_and_manual_unavailable(catalog,monkeypatch):
    c,ident=catalog
    c.edit(ident,1,{'text':'人間が校正した本文'})
    c.edit(ident,2,{'included':False})
    c.edit(ident,4,{'selection_override':True})
    seen=[]
    monkeypatch.setattr(c,'llm',lambda doc,settings,selected,texts:seen.append((selected,texts)))
    c.start_llm(ident,LLMSettings())
    for _ in range(100):
        if not c.doc(ident)['busy']:break
        time.sleep(.01)
    assert seen[0][0]==[1,4]
    assert seen[0][1][1]=='人間が校正した本文'
    assert c.page(ident,4)['quality']=='UNAVAILABLE'
    c.start_llm(ident,LLMSettings())
    for _ in range(100):
        if not c.doc(ident)['busy']:break
        time.sleep(.01)
    assert len(seen)==2 and seen[1][0]==[1,4]


def test_llm_progress_tracks_request_and_failure(catalog):
    c,ident=catalog
    observed=[]
    class Progress(LLM):
        def health(self):
            assert c.doc(ident)['llm_progress']['status']=='connecting'
        def request_edits(self,*args):
            observed.append(c.doc(ident)['llm_progress'])
            if len(observed)==2:raise RuntimeError('失敗テスト')
            return {'edits':[]}
    c.llm(ident,LLMSettings(),[1,3],{1:'本文です。',3:'次の本文です。'},Progress())
    assert [(p['page'],p['done'],p['chunk']) for p in observed]==[(1,0,1),(3,1,1)]
    assert all(p['request_started_at'] for p in observed)
    progress=c.doc(ident)['llm_progress']
    assert progress['status']=='completed'
    assert progress['done']==2 and progress['failed']==1 and progress['total']==2
    assert progress['finished_at']
