"""Human-triggered document operations."""
import base64
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional
import cv2
import pymupdf
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from .catalog import Catalog
from .core import inspect_pdf
from .crop_preview import preview_page
from .ocr import OCRSettings, render_page
from .narration import LLMSettings


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')
class OCRRequest(Strict):
    settings: OCRSettings
    direction: str='vertical'
class QualityRequest(Strict):
    percentile: float=Field(default=10,ge=1,le=100)
    minimum: int=Field(default=20,ge=1,le=100000)
    low: float=Field(default=50,ge=0,le=100)
    very_low: float=Field(default=20,ge=0,le=100)
class EditRequest(Strict):
    expected_revision: Optional[int]=None
    text: Optional[str]=None
    included: Optional[bool]=None
    human_checked: Optional[bool]=None
    selection_override: Optional[bool]=None
class AudioRequest(Strict):
    status: str


def routes(app):
    router=APIRouter(prefix='/api/documents')
    def catalog():return app.state.catalog
    def with_final_file(doc):
        exists=(catalog().folder(doc['id'])/'text/book_final.txt').is_file()
        return {**doc,'final_exists':exists,'final_current':bool(doc.get('final_current') and exists)}
    @router.get('/storage')
    def storage():return {'root':str(catalog().root.resolve())}
    @router.get('')
    def listing():return [with_final_file(d) for d in catalog().list()]
    @router.post('',status_code=201)
    def add(file:UploadFile=File(...)):
        try:return catalog().add(file.filename or '',file.file)
        finally:file.file.close()
    @router.get('/{ident}')
    def get(ident:str):return with_final_file(catalog().doc(ident))
    @router.get('/{ident}/pages')
    def pages(ident:str):
        return [{k:v for k,v in p.items() if k!='units'} for p in catalog().pages(ident)]
    @router.get('/{ident}/pages/{number}')
    def page(ident:str,number:int):
        result=catalog().page(ident,number);result.pop('units',None)
        return {**result,'text':catalog().text(ident,number)}
    @router.patch('/{ident}/pages/{number}')
    def edit(ident:str,number:int,body:EditRequest):
        changes=body.model_dump(exclude_unset=True)
        if any(changes.get(k) is None for k in changes if k!='selection_override'):raise ValueError('入力値がありません。')
        return catalog().edit(ident,number,changes)
    @router.put('/{ident}/quality')
    def quality(ident:str,body:QualityRequest):
        with catalog().lock:
            catalog().idle(ident);catalog().rank(ident,body.model_dump())
        return catalog().doc(ident)
    @router.post('/{ident}/ocr',status_code=202)
    def ocr(ident:str,body:OCRRequest):return catalog().start_ocr(ident,body.settings,body.direction)
    @router.post('/{ident}/regenerate-text')
    def regenerate(ident:str):catalog().regenerate(ident);return catalog().doc(ident)
    @router.post('/{ident}/llm',status_code=202)
    def llm(ident:str,body:dict):
        try:settings=LLMSettings(**body)
        except TypeError as exc:raise ValueError('LLM設定が不正です。') from exc
        return catalog().start_llm(ident,settings)
    @router.post('/{ident}/final')
    def final(ident:str):catalog().final(ident);return catalog().doc(ident)
    @router.get('/{ident}/final')
    def download(ident:str):
        if not catalog().doc(ident).get('final_current'):raise ValueError('現在の本文から最終TXTを生成してください。')
        path=catalog().folder(ident)/'text/book_final.txt'
        if not path.exists():raise FileNotFoundError(str(path))
        return FileResponse(path,filename='book_final.txt')
    @router.post('/{ident}/audio-decision')
    def audio(ident:str,body:AudioRequest):
        if body.status not in ('approved','abandoned','pending'):raise ValueError('音声化判断が不正です。')
        with catalog().lock:
            doc=catalog().idle(ident)
            if body.status=='approved' and not doc.get('final_current'):raise ValueError('先に最終TXTを生成してください。')
            doc['audio_conversion_status']=body.status;catalog().save_doc(doc)
        return doc
    @router.delete('/{ident}/assets/{kind}')
    def remove(ident:str,kind:str):catalog().remove(ident,kind);return catalog().doc(ident)
    @router.get('/{ident}/preview/{number}')
    def preview(ident:str,number:int):return preview_page(catalog().folder(ident)/'source/book.pdf',number)
    @router.get('/{ident}/review-image/{number}')
    def review_image(ident:str,number:int):
        page=catalog().page(ident,number)
        path=catalog().folder(ident)/'source/book.pdf'
        if not path.exists():raise ValueError('PDFが削除されています。本文は編集できますが原画像は表示できません。')
        with pymupdf.open(path) as pdf:
            if not 1<=number<=len(pdf):raise ValueError('現在のPDFに該当ページがありません。')
            settings=OCRSettings(**page['crop']);settings=OCRSettings(**{**settings.to_dict(),'dpi':min(140,settings.dpi)})
            image=render_page(pdf[number-1],settings)
            png=cv2.imencode('.png',image)[1]
            return {'image':'data:image/png;base64,'+base64.b64encode(png).decode()}
    @router.post('/{ident}/pdf')
    def replace_pdf(ident:str,file:UploadFile=File(...)):
        try:
            with catalog().lock:
                doc=catalog().idle(ident)
                with tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp)/'book.pdf';size=0
                    with path.open('wb') as out:
                        while True:
                            chunk=file.file.read(1024*1024)
                            if not chunk:break
                            size+=len(chunk)
                            if size>512*1024*1024:raise ValueError('PDFは512MB以内にしてください。')
                            out.write(chunk)
                    count=inspect_pdf(path);target=catalog().folder(ident)/'source/book.pdf';target.parent.mkdir(exist_ok=True);shutil.copyfile(path,target)
                doc.update(page_count=count,pdf_replaced=True);doc['assets']['pdf']=True;catalog().save_doc(doc)
                return doc
        finally:file.file.close()
    app.include_router(router)
