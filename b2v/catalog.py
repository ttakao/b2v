"""One document per PDF; independent human-triggered operations, no document phases."""
import json
import re
import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import pymupdf
from .core import inspect_pdf
from .ocr import OCRSettings, render_page, detect_direction, effective_direction
from .ocr_confidence import ConfidenceOCR
from .ocr_cleanup import normalize_ocr_text
from .ocr_workflow import atomic_text
from .page_selection import selected_pages, parse_pages
from .quality import statistics, classify
from .narration import LLMSettings, LlamaNarrator, split_text, apply_edits


def now(): return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.executor=ThreadPoolExecutor(max_workers=1)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS pages (document_id TEXT, number INTEGER, data TEXT NOT NULL, PRIMARY KEY(document_id, number))')
            for ident,data in db.execute('SELECT id,data FROM documents').fetchall():
                doc=json.loads(data)
                if doc.get('tts_sample_status')=='generating':
                    doc.update(tts_sample_status='failed',tts_sample_error='前回のサンプル生成が中断されました。再度生成してください。')
                    db.execute('UPDATE documents SET data=? WHERE id=?',(json.dumps(doc,ensure_ascii=False),ident))
                if doc.get('busy'):
                    doc.update(busy=None,error='前回の処理が中断されました。必要な処理を再実行してください。')
                    db.execute('UPDATE documents SET data=? WHERE id=?',(json.dumps(doc,ensure_ascii=False),ident))
    def connect(self): return sqlite3.connect(self.root/'catalog.sqlite3',timeout=30)
    def folder(self, ident):
        if not re.fullmatch('[a-f0-9]{32}',ident): raise FileNotFoundError(ident)
        return self.root/'books'/ident
    def doc(self, ident):
        self.folder(ident)
        with self.connect() as db: row=db.execute('SELECT data FROM documents WHERE id=?',(ident,)).fetchone()
        if not row: raise FileNotFoundError(ident)
        return json.loads(row[0])
    def save_doc(self, doc):
        with self.connect() as db: db.execute('INSERT OR REPLACE INTO documents VALUES (?,?)',(doc['id'],json.dumps(doc,ensure_ascii=False)))
    def list(self):
        with self.connect() as db: return [json.loads(r[0]) for r in db.execute('SELECT data FROM documents ORDER BY rowid DESC')]
    def pages(self, ident):
        self.doc(ident)
        with self.connect() as db: return [json.loads(r[0]) for r in db.execute('SELECT data FROM pages WHERE document_id=? ORDER BY number',(ident,))]
    def page(self, ident, number):
        with self.connect() as db: row=db.execute('SELECT data FROM pages WHERE document_id=? AND number=?',(ident,number)).fetchone()
        if not row: raise FileNotFoundError('ページがありません。')
        return json.loads(row[0])
    def save_page(self, ident, page):
        with self.connect() as db: db.execute('INSERT OR REPLACE INTO pages VALUES (?,?,?)',(ident,page['page_number'],json.dumps(page,ensure_ascii=False)))
    def idle(self,ident):
        doc=self.doc(ident)
        if doc.get('busy'): raise ValueError('処理中です。完了してから操作してください。')
        return doc
    def add(self,name,stream):
        if not name.lower().endswith('.pdf'): raise ValueError('PDFを指定してください。')
        ident=uuid.uuid4().hex;folder=self.folder(ident);(folder/'source').mkdir(parents=True)
        try:
            total=0
            for kind in ('ocr','text','audio'):(folder/kind).mkdir()
            with (folder/'source/book.pdf').open('wb') as output:
                while True:
                    block=stream.read(1024*1024)
                    if not block:break
                    total+=len(block)
                    if total>512*1024*1024:raise ValueError('PDFは512MB以内にしてください。')
                    output.write(block)
            count=inspect_pdf(folder/'source/book.pdf')
            doc={'id':ident,'name':Path(name).name,'page_count':count,'created_at':now(),'busy':None,'error':None,
                 'ocr_settings':OCRSettings().to_dict(),'direction':'vertical','quality_settings':{'percentile':10,'minimum':20,'low':50,'very_low':20},
                 'assets':{'pdf':True,'ocr':False,'text':False,'audio':False},'final_current':False,'audio_conversion_status':'pending'}
            self.save_doc(doc);return doc
        except Exception:shutil.rmtree(folder);raise
    def text_path(self,ident,number): return self.folder(ident)/'text'/f'page_{number:04d}.txt'
    def text(self,ident,number):
        path=self.text_path(ident,number)
        return path.read_text(encoding='utf-8') if path.exists() else None
    def log(self,ident,event):
        folder=self.folder(ident)/'text';folder.mkdir(exist_ok=True)
        with (folder/'llm_requests.jsonl').open('a',encoding='utf-8') as out:out.write(json.dumps({'timestamp':now(),**event},ensure_ascii=False)+'\n')
    def invalidate(self,doc): doc.update(final_current=False,audio_conversion_status='pending',audio_stale=doc['assets'].get('audio',False))
    def run(self,ident,kind,task):
        with self.lock:
            doc=self.idle(ident);doc.update(busy=kind,error=None,processed=0);self.save_doc(doc)
            self.executor.submit(self._work,ident,task)
        return self.doc(ident)
    def _work(self,ident,task):
        try:task()
        except Exception as exc:
            with self.lock:
                doc=self.doc(ident);doc['error']=str(exc)
                if doc.get('busy')=='LLM' and doc.get('llm_progress'):doc['llm_progress'].update(status='failed',error=str(exc),finished_at=now())
                self.save_doc(doc)
        finally:
            with self.lock:
                doc=self.doc(ident);doc['busy']=None;self.save_doc(doc)
    def rank(self,ident,settings=None):
        with self.lock:
            doc=self.doc(ident);q=settings or doc['quality_settings']
            if not 0<=q['very_low']<=q['low']<=100:raise ValueError('confidence閾値が不正です。')
            pages=self.pages(ident)
            for page in pages:page.update(statistics(page.get('units',[]),q['low'],q['very_low']))
            boundary=classify(pages,q['percentile'],q['minimum'])
            for page in pages:
                override=page.get('selection_override')
                page['llm_selected']=(page['auto_selected'] if override is None else override) and page.get('included',True)
                self.save_page(ident,page)
            doc.update(quality_settings=q,review_boundary=boundary);self.save_doc(doc)
    def start_ocr(self,ident,settings,direction):
        settings.validate()
        if direction not in ('vertical','horizontal','auto'):raise ValueError('書字方向が不正です。')
        if not (self.folder(ident)/'source/book.pdf').exists():raise ValueError('PDFがありません。')
        return self.run(ident,'OCR',lambda:self.ocr(ident,settings,direction))
    def ocr(self,ident,settings,direction,provider=None):
        provider=provider or ConfidenceOCR();provider.check()
        folder=self.folder(ident);raw_folder=folder/'ocr';raw_folder.mkdir(exist_ok=True)
        doc=self.doc(ident);doc.update(ocr_settings=settings.to_dict(),direction=direction);self.save_doc(doc)
        with pymupdf.open(folder/'source/book.pdf') as pdf:
            numbers=selected_pages(settings,len(pdf));doc['operation_total']=len(numbers);self.save_doc(doc)
            for number in numbers:
                image=render_page(pdf[number-1],settings);detection=detect_direction(image)
                forced='horizontal' if number in parse_pages(settings.horizontal_pages) else 'vertical' if number in parse_pages(settings.vertical_pages) else None
                actual=forced or effective_direction(detection,direction,settings.threshold)
                result=provider.recognize_with_confidence(image,actual,settings.dpi,settings.horizontal_mode=='blocks')
                raw=result['text'];atomic_text(raw_folder/f'page_{number:04d}.txt',raw)
                atomic_text(raw_folder/f'page_{number:04d}_tsv.json',json.dumps(result['tsvs'],ensure_ascii=False))
                cleanup=normalize_ocr_text(raw)
                try:page=self.page(ident,number)
                except FileNotFoundError:page={'page_number':number,'included':True,'human_checked':False,'human_saved_at':None,'llm_attempted_at':None,'selection_override':None}
                page.update(units=result['units'],writing_direction_confidence=detection['confidence'],detected_direction=detection['direction'],direction=actual,
                            crop=settings.to_dict(),ocr_at=now(),cleanup_stats=cleanup['stats'],ocr_available=True)
                page.update(statistics(result['units'],doc['quality_settings']['low'],doc['quality_settings']['very_low']))
                page.setdefault('quality','PENDING');page.setdefault('candidate',False);page.setdefault('llm_selected',False)
                path=self.text_path(ident,number)
                if not path.exists():atomic_text(path,cleanup['text']);page.update(text_stale=False,text_origin='cleanup',human_checked=False,text_revision=page.get('text_revision',0)+1)
                else:page['text_stale']=True
                self.save_page(ident,page)
                doc=self.doc(ident);doc['processed']=doc.get('processed',0)+1;doc['assets'].update(ocr=True,text=True);self.invalidate(doc);self.save_doc(doc)
        self.rank(ident)
    def regenerate(self,ident):
        with self.lock:
            doc=self.idle(ident);count=0
            for page in self.pages(ident):
                path=self.folder(ident)/'ocr'/f"page_{page['page_number']:04d}.txt"
                if not path.exists():continue
                result=normalize_ocr_text(path.read_text())
                atomic_text(self.text_path(ident,page['page_number']),result['text'])
                page.update(human_checked=False,text_stale=False,text_origin='cleanup',cleanup_stats=result['stats'],text_revision=page.get('text_revision',0)+1);self.save_page(ident,page);count+=1
            if not count:raise ValueError('再生成できるOCR本文がありません。')
            doc['assets']['text']=True;self.invalidate(doc);self.save_doc(doc)
    def edit(self,ident,number,changes):
        with self.lock:
            doc=self.idle(ident);page=self.page(ident,number)
            if 'text' in changes:
                if not isinstance(changes['text'],str):raise ValueError('本文は文字列で指定してください。')
                if 'expected_revision' in changes and changes['expected_revision']!=page.get('text_revision',0):raise ValueError('本文が更新されました。ページを開き直して確認してください。')
                atomic_text(self.text_path(ident,number),changes['text']);page.update(human_saved_at=now(),human_checked=True,text_origin='human',text_revision=page.get('text_revision',0)+1);doc['assets']['text']=True;self.invalidate(doc)
            for key in ('human_checked','included'):
                if key in changes:
                    page[key]=changes[key]
                    if key=='included':self.invalidate(doc)
            if 'selection_override' in changes:page['selection_override']=changes['selection_override']
            page['llm_selected']=(page.get('auto_selected',False) if page.get('selection_override') is None else page['selection_override']) and page['included']
            self.save_page(ident,page);self.save_doc(doc)
            return page
    def start_llm(self,ident,settings):
        settings.validate()
        with self.lock:
            self.idle(ident)
            pages=self.pages(ident);selected=[p['page_number'] for p in pages if p.get('llm_selected') and p['included']]
            if not selected:raise ValueError('LLM対象ページを選択してください。')
            texts={p['page_number']:self.text(ident,p['page_number']) for p in pages}
            if any(texts[n] is None for n in selected):raise ValueError('選択ページのテキストがありません。OCRから再生成してください。')
            return self.run(ident,'LLM',lambda:self.llm(ident,settings,selected,texts))
    def llm(self,ident,settings,selected,texts,provider=None):
        doc=self.doc(ident);doc.update(processed=0,operation_total=len(selected),llm_settings=settings.to_dict(),llm_progress={'status':'connecting','started_at':now(),'total':len(selected),'done':0,'failed':0});self.save_doc(doc)
        provider=provider or LlamaNarrator(settings);provider.health()
        for number in selected:
            page=self.page(ident,number);page.update(llm_attempted_at=now(),llm_status='running');self.save_page(ident,page)
            try:
                chunks=split_text(texts[number],settings.chunk_chars);results=[]
                for index,current in enumerate(chunks):
                    doc=self.doc(ident);doc['llm_progress'].update(status='generating',page=number,chunk=index+1,chunks=len(chunks),request_started_at=now());self.save_doc(doc)
                    before=''.join(chunks[:index]) or texts.get(number-1) or ''
                    after=''.join(chunks[index+1:]) or texts.get(number+1) or ''
                    provider.metrics_sink=lambda metrics:self.log(ident,{'page':number,'event':'llm_request',**metrics})
                    response=provider.request_edits(current,before[-settings.context_chars:] if settings.context_chars else '',after[:settings.context_chars])
                    result=apply_edits(current,response['edits']);results.append(result)
                    self.log(ident,{'page':number,'chunk':index+1,'event':'edits','applied':result['applied_edit_count'],'rejected':result['rejected_edit_count'],'warnings':result['warnings']})
                text=''.join(r['narration'] for r in results)
                atomic_text(self.text_path(ident,number),text)
                # Latest diff only, not a history or a restore mechanism.
                atomic_text(self.folder(ident)/'text'/f'page_{number:04d}_llm.json',json.dumps(results,ensure_ascii=False))
                page.update(llm_status='completed',llm_completed_at=now(),human_checked=False,text_origin='llm',text_revision=page.get('text_revision',0)+1,
                            applied_edits=sum(r['applied_edit_count'] for r in results),rejected_edits=sum(r['rejected_edit_count'] for r in results),warnings=[w for r in results for w in r['warnings']])
            except Exception as exc:page.update(llm_status='failed',warnings=[str(exc)])
            self.save_page(ident,page)
            doc=self.doc(ident);doc['processed']=doc.get('processed',0)+1
            doc['llm_progress'].update(done=doc['processed'],failed=doc['llm_progress']['failed']+(page['llm_status']=='failed'))
            self.invalidate(doc);self.save_doc(doc)
        doc=self.doc(ident);doc['llm_progress'].update(status='completed',finished_at=now());self.save_doc(doc)
    def final(self,ident):
        with self.lock:
            doc=self.idle(ident);texts=[]
            for page in self.pages(ident):
                if not page['included']:continue
                text=self.text(ident,page['page_number'])
                if text is None:raise ValueError(f"PDF {page['page_number']}ページのテキストがありません。")
                texts.append(text)
            if not texts:raise ValueError('出力対象ページがありません。')
            atomic_text(self.folder(ident)/'text/book_final.txt','\n\n'.join(texts))
            doc.update(final_current=True);self.save_doc(doc)
            return self.folder(ident)/'text/book_final.txt'
    def remove(self,ident,kind):
        if kind not in ('pdf','ocr','text','audio'):raise ValueError('ファイル種別が不正です。')
        with self.lock:
            doc=self.idle(ident);folder=self.folder(ident)/{'pdf':'source','ocr':'ocr','text':'text','audio':'audio'}[kind]
            if kind=='audio':
                if doc.get('tts_sample_status')=='generating':raise ValueError('サンプル生成中です。完了してから音声を削除してください。')
                doc.pop('tts_sample',None);doc.pop('tts_sample_status',None);doc.pop('tts_sample_error',None)
                doc.pop('audio_result',None);doc.pop('audio_run',None);doc['audio_stale']=False
                for key in ('mp3_result','mp3_run','mp3_cleanup'):doc.pop(key,None)
                with self.connect() as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='audio_chunks'").fetchone():
                        db.execute('DELETE FROM audio_chunks WHERE document_id=?',(ident,))
            if folder.exists():shutil.rmtree(folder)
            doc['assets'][kind]=False
            for page in self.pages(ident):
                if kind=='ocr':page.update(ocr_available=False,units=[])
                if kind=='text':page.update(human_checked=False,text_origin=None,llm_status='text_deleted')
                self.save_page(ident,page)
            if kind in ('ocr','text'):self.invalidate(doc)
            self.save_doc(doc)
            if kind=='ocr':self.rank(ident)
            return self.prune_empty(ident)

    def prune_empty(self, ident):
        """Remove only idle documents with no assets or remaining content files."""
        with self.lock:
            doc=self.idle(ident)
            if any(doc.get('assets',{}).values()):return False
            folder=self.folder(ident)
            files=[p for p in folder.rglob('*') if p.is_file()]
            logs={'generation.jsonl','mp3.log.jsonl'}
            if any(p.parent!=folder/'audio' or p.name not in logs for p in files):return False
            with self.connect() as db:
                db.execute('DELETE FROM pages WHERE document_id=?',(ident,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='audio_chunks'").fetchone():
                    db.execute('DELETE FROM audio_chunks WHERE document_id=?',(ident,))
                db.execute('DELETE FROM documents WHERE id=?',(ident,))
            if folder.exists():shutil.rmtree(folder)
            return True
