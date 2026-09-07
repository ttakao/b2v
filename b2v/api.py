import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from .application import Jobs
from .catalog import Catalog
from .document_api import routes
from .tts_api import routes as tts_routes
from .audio_workflow import AudioWorkflow
from .mp3 import MP3Workflow
from .crop_preview import preview_page
from .core import inspect_pdf
from .ocr import OCRSettings, TesseractOCR
from .narration import LLMSettings, LlamaNarrator
from pydantic import BaseModel, ConfigDict, Field
from .config import data_dir, llm_url, service_url
from urllib.parse import urlsplit
from pypdf.errors import PdfReadError

class OCRRerunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    direction: Literal['auto', 'vertical', 'horizontal'] = 'vertical'
    settings: OCRSettings


class NarrationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str = Field(default_factory=llm_url)
    model: str = 'b2v-qwen3'
    temperature: float = .2
    chunk_chars: int = 1000
    context_chars: int = 150
    prompt: str = LLMSettings().prompt


STATIC = Path(__file__).parent / 'static'


def create_app(work_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.jobs = Jobs(Path(work_dir or os.environ.get('B2V_WORK_DIR', Path(__file__).parent.parent / 'work')))
        app.state.catalog = Catalog(Path(work_dir)/'catalog' if work_dir else data_dir())
        app.state.audio = AudioWorkflow(app.state.catalog)
        app.state.audio.remove_samples()
        app.state.mp3 = MP3Workflow(app.state.catalog)
        app.state.preview_dir = tempfile.TemporaryDirectory(prefix='b2v-preview-')
        yield
        app.state.audio.stop()
        app.state.catalog.executor.shutdown(wait=True)
        app.state.preview_dir.cleanup()
        app.state.jobs.executor.shutdown(wait=True)

    app = FastAPI(title='b2v — PDF オーディオブック変換', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver', urlsplit(service_url('api')).hostname])
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail':str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={'detail': 'ジョブまたは成果物が見つかりません。'})

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.post('/api/previews', status_code=201)
    def upload_preview(file: UploadFile = File(...)):
        token = uuid.uuid4().hex
        path = Path(app.state.preview_dir.name) / (token + '.pdf')
        try:
            if not (file.filename or '').lower().endswith('.pdf'):
                raise ValueError('PDFファイルを指定してください。')
            total = 0
            with path.open('wb') as output:
                while chunk := file.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ValueError('PDFは512MB以内にしてください。')
                    output.write(chunk)
            pages = inspect_pdf(path)
            return {'id':token, 'pages':pages}
        except (ValueError, PdfReadError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        finally:
            file.file.close()

    @app.delete('/api/previews/{token}')
    def delete_preview(token: str):
        if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise HTTPException(404, 'プレビューがありません。')
        (Path(app.state.preview_dir.name) / (token+'.pdf')).unlink(missing_ok=True)
        return {'deleted':True}

    @app.get('/api/previews/{token}/pages/{page}')
    def get_preview(token: str, page: int):
        if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise HTTPException(404, 'プレビューがありません。')
        try:
            return preview_page(Path(app.state.preview_dir.name) / (token+'.pdf'), page)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/jobs/{job_id}/crop-preview/{book}/{page}')
    def existing_preview(job_id: str, book: int, page: int):
        job = app.state.jobs.get(job_id)
        if job.get('mode') != 'ocr' or not 1 <= book <= len(job['books']):
            raise HTTPException(404, 'OCRのPDFがありません。')
        try:
            return preview_page(app.state.jobs.directory(job_id) / job['books'][book-1]['source'], page)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/jobs')
    def list_jobs():
        return app.state.jobs.list()

    @app.post('/api/jobs', status_code=202)
    def create_job(files: List[UploadFile] = File(...),
                   direction: Literal['auto', 'vertical', 'horizontal'] = Form('vertical'),
                   mode: Literal['receipt', 'ocr'] = Form('ocr'),
                   dpi: int = Form(300), top: float = Form(3), bottom: float = Form(3),
                   left: float = Form(3), right: float = Form(3), threshold: float = Form(.75),
                   pages: str = Form(''), exclude_pages: str = Form(''),
                   horizontal_pages: str = Form(''), vertical_pages: str = Form(''),
                   horizontal_mode: Literal['blocks','page'] = Form('blocks')):
        try:
            if not 1 <= len(files) <= 20:
                raise ValueError('一度に選択できるPDFは1〜20冊です。')
            return app.state.jobs.create([(file.filename, file.file) for file in files], direction, mode,
                                         OCRSettings(dpi, top, bottom, left, right, threshold, pages, exclude_pages,
                                                     horizontal_pages, vertical_pages, horizontal_mode))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            for file in files:
                file.file.close()

    @app.post('/api/jobs/{job_id}/reprocess-ocr', status_code=202)
    def reprocess_ocr(job_id: str, body: OCRRerunRequest):
        try:
            return app.state.jobs.reprocess_ocr(job_id, body.direction, body.settings)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/llm-defaults')
    def llm_defaults():
        return LLMSettings().to_dict()

    @app.post('/api/llm-health')
    def llm_health(settings: NarrationRequest):
        try:
            return LlamaNarrator(LLMSettings(**settings.model_dump())).health()
        except (ValueError, RuntimeError) as exc:
            return {'ready':False, 'message':str(exc)}

    @app.post('/api/jobs/{job_id}/narration', status_code=202)
    def start_narration(job_id: str, settings: NarrationRequest):
        try:
            return app.state.jobs.start_narration(job_id, LLMSettings(**settings.model_dump()))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get('/api/jobs/{job_id}/narration/{book}/{page}')
    def narration_page(job_id: str, book: int, page: int):
        return app.state.jobs.narration_page(job_id, book, page)

    @app.get('/api/ocr-health')
    def ocr_health():
        try:
            return TesseractOCR().check()
        except RuntimeError as exc:
            return {'ready': False, 'message': str(exc)}

    @app.get('/api/jobs/{job_id}/pages/{book}/{page}')
    def page_result(job_id: str, book: int, page: int):
        return app.state.jobs.page_result(job_id, book, page)

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str):
        return app.state.jobs.get(job_id)

    @app.delete('/api/jobs/{job_id}')
    def delete_job(job_id: str):
        try:
            return app.state.jobs.delete(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/jobs/{job_id}/retry', status_code=202)
    def retry(job_id: str):
        try:
            return app.state.jobs.retry(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/jobs/{job_id}/files/{name:path}')
    def download(job_id: str, name: str):
        path = app.state.jobs.artifact(job_id, name)
        return FileResponse(path, filename=path.name)

    routes(app)
    tts_routes(app)
    return app


app = create_app()
