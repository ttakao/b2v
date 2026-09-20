"""Launch existing native services with the shared URL configuration."""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from .config import service_url


def command(service):
    if service not in ('api','llm'):raise ValueError('起動できるサービスはapiとllmです。')
    url = urlsplit(service_url(service))
    if url.scheme != 'http':raise ValueError('付属起動スクリプトはHTTP用です。HTTPSは別途終端してください。')
    host, port = url.hostname, str(url.port or 80)
    if service=='api':return [sys.executable,'-m','uvicorn','b2v.api:app','--host',host,'--port',port]
    if service=='llm':
        model = os.environ.get('B2V_MODEL_PATH','/Users/tsukasa_takao/dev/models/qwen3-14b-q4_k_m.gguf')
        if not Path(model).is_file():raise ValueError('モデルが見つかりません: '+model)
        return ['llama-server','--model',model,'--alias','b2v-qwen3','--host',host,'--port',port,'--ctx-size','8192','--parallel','1','--gpu-layers','all','--jinja','--reasoning','off','--chat-template-kwargs','{"enable_thinking":false}']


if __name__=='__main__':
    service=sys.argv[1]
    args=command(service)
    os.execvp(args[0],args)
