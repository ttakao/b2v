"""Document-linked Google Neural2 WAV operations."""
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from .google_tts import GoogleClient, GoogleSettings, Usage


class WAVRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    settings: GoogleSettings
    max_chunk_chars: int = Field(default=300,ge=50,le=300)


class AuditionRequest(BaseModel):
    settings: GoogleSettings
    text: str = Field(min_length=1, max_length=150)


def routes(app):
    router = APIRouter()

    @router.post('/api/tts/audition')
    def audition(body:AuditionRequest):
        if not body.text.strip():raise ValueError('試聴する文章を入力してください。')
        audio=app.state.audio
        if not audio.guard.acquire(blocking=False):raise ValueError('音声生成中です。完了後に試聴してください。')
        try:
            if audio.active:raise ValueError('本のWAV生成中は試聴できません。')
            data, _ = audio.client(body.settings).synthesize(body.text, body.settings)
            return Response(data,media_type='audio/wav',headers={'Cache-Control':'no-store'})
        finally:audio.guard.release()

    @router.get('/api/tts/health')
    def health():
        client = GoogleClient(app.state.catalog.root)
        try:return {'ready':True, **client.health()}
        except ValueError as exc:return {'ready':False,'message':str(exc)}

    @router.get('/api/tts/models')
    def models():
        client = GoogleClient(app.state.catalog.root)
        return client.list_models()

    @router.get('/api/tts/usage')
    def usage():return Usage(app.state.catalog.root).status()

    @router.post('/api/documents/{ident}/tts/estimate')
    def estimate(ident:str,body:WAVRequest):
        return app.state.audio.estimate(ident,body.settings,body.max_chunk_chars)

    @router.get('/api/documents/{ident}/tts')
    def settings(ident:str):
        doc=app.state.catalog.doc(ident)
        return {'settings':doc.get('tts_settings'),'max_chunk_chars':doc.get('tts_max_chars',300),
                'run':doc.get('audio_run'),'result':doc.get('audio_result'),'stale':doc.get('audio_stale',False)}

    @router.put('/api/documents/{ident}/tts')
    def save(ident:str,body:WAVRequest):
        with app.state.catalog.lock:
            doc=app.state.catalog.idle(ident)
            doc.update(tts_settings=body.settings.model_dump(),tts_max_chars=body.max_chunk_chars)
            app.state.catalog.save_doc(doc)
        return settings(ident)

    @router.post('/api/documents/{ident}/tts/wav',status_code=202)
    def generate(ident:str,body:WAVRequest):
        app.state.audio.start(ident,body.settings,body.max_chunk_chars)
        return settings(ident)

    @router.post('/api/documents/{ident}/tts/stop')
    def stop(ident:str):
        app.state.catalog.doc(ident)
        app.state.audio.stop(ident)
        return settings(ident)

    @router.get('/api/documents/{ident}/tts/wav')
    def wav(ident:str,download:bool=False):
        doc=app.state.catalog.doc(ident);result=doc.get('audio_result')
        if not result:raise FileNotFoundError('完成したWAVがありません。')
        path=app.state.catalog.folder(ident)/'audio'/result['filename']
        if not path.is_file():raise FileNotFoundError('完成したWAVがありません。')
        return FileResponse(path,media_type='audio/wav',filename='book.wav' if download else None,headers={'Cache-Control':'no-store'})

    @router.post('/api/documents/{ident}/tts/mp3', status_code=202)
    def mp3_generate(ident:str):
        app.state.mp3.start(ident)
        return app.state.catalog.doc(ident)

    @router.delete('/api/documents/{ident}/tts/mp3')
    def mp3_delete(ident:str):
        app.state.mp3.delete(ident)
        return {'deleted':True}

    @router.get('/api/documents/{ident}/tts/mp3')
    def mp3_get(ident:str, download:bool=False):
        result=app.state.catalog.doc(ident).get('mp3_result')
        if not result:raise FileNotFoundError('MP3がありません。')
        path=app.state.catalog.folder(ident)/'audio'/result['filename']
        if not path.is_file():raise FileNotFoundError('MP3がありません。')
        return FileResponse(path,media_type='audio/mpeg',filename=result['display_name'] if download else None,headers={'Cache-Control':'no-store'})

    app.include_router(router)
