"""ディスク永続化と単一ワーカーによるジョブ実行。"""
import json
import re
import shutil
import threading
import uuid
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from .core import inspect_pdf, receipt
from .ocr import OCRSettings
from .page_selection import book_pages
from .ocr_workflow import process_ocr
from .narration import LLMSettings
from .narration_workflow import process_narration

MAX_FILE_BYTES = 512 * 1024 * 1024


class Jobs:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        for path in root.glob('*/metadata.json'):
            job = json.loads(path.read_text())
            if job['status'] in ('queued', 'running'):
                job['status'] = 'interrupted'
                job['error'] = {'phase': job.get('phase', 'receipt'),
                                'page': job.get('position', {}).get('page'),
                                'book': job.get('position', {}).get('book'), 'segment': None,
                                'chunk': job.get('position', {}).get('chunk'), 'message': '前回の処理が中断されました。再実行できます。'}
                self.save(job)

    def directory(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id):
            raise FileNotFoundError(job_id)
        return self.root / job_id

    def save(self, job):
        with self.lock:
            path = self.directory(job['id']) / 'metadata.json'
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(path)

    def get(self, job_id):
        with self.lock:
            return json.loads((self.directory(job_id) / 'metadata.json').read_text())

    def list(self):
        with self.lock:
            return sorted([json.loads(p.read_text()) for p in self.root.glob('*/metadata.json')],
                          key=lambda j: j['created_at'], reverse=True)

    def create(self, files, direction, mode="ocr", settings=None):
        settings = (settings or OCRSettings()).validate()
        job_id = uuid.uuid4().hex
        directory = self.directory(job_id)
        directory.mkdir()
        books = []
        try:
            for index, (name, stream) in enumerate(files, 1):
                name = (name or 'book.pdf').replace('\\', '/').split('/')[-1]
                if not name.lower().endswith('.pdf'):
                    raise ValueError('PDFファイルを選択してください。')
                relative = f'source/{index:03d}.pdf'
                path = directory / relative
                path.parent.mkdir(exist_ok=True)
                size = 0
                with path.open('wb') as output:
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_FILE_BYTES:
                            raise ValueError('PDFは1ファイル512MB以内にしてください。')
                        output.write(block)
                books.append({'name': name, 'source': relative, 'bytes': size,
                              'status': 'queued', 'pages': None})
            job = {'id': job_id, 'created_at': datetime.now(timezone.utc).isoformat(),
                   'status': 'queued', 'phase': 'receipt', 'completed': 0,
                   'total': len(books), 'direction': direction, 'books': books,
                   'artifacts': [], 'error': None, 'mode': mode,
                   'ocr_settings': settings.to_dict(), 'ocr_completed': 0, 'ocr_total': 0}
            self.save(job)
            self.executor.submit(self.run, job_id)
            return job
        except Exception:
            shutil.rmtree(directory)
            raise

    def retry(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job['status'] not in ('failed', 'interrupted'):
                raise ValueError('失敗または中断したジョブだけ再実行できます。')
            job['status'] = 'queued'
            job['error'] = None
            self.save(job)
            self.executor.submit(self.run, job_id)
            return job

    def run(self, job_id):
        job = self.get(job_id)
        directory = self.directory(job_id)
        try:
            job['status'] = 'running'
            self.save(job)
            if job.get('mode') == 'narration':
                process_narration(job, directory, self.directory(job['ocr_parent']), self.save)
            elif job.get('mode') == 'ocr':
                process_ocr(job, directory, self.save)
            else:
                for index, book in enumerate(job['books'], 1):
                    if book['status'] == 'completed':
                        continue
                    pages = inspect_pdf(directory / book['source'])
                    report = f'results/{index:03d}_receipt.txt'
                    (directory / 'results').mkdir(exist_ok=True)
                    (directory / report).write_text(receipt(book['name'], pages, job['direction']), encoding='utf-8')
                    book.update(status='completed', pages=pages)
                    job['artifacts'].append({'path': report, 'label': f"{book['name']} — 受付レポート"})
                    job['completed'] += 1
                    self.save(job)
            job['phase'] = 'packaging'
            self.save(job)
            # Include per-page raw text and detection metadata for OCR jobs.
            with ZipFile(directory / 'results.zip.tmp', 'w', ZIP_DEFLATED) as archive:
                for item in job['artifacts']:
                    archive.write(directory / item['path'], item['path'])
                if job.get('mode') in ('ocr', 'narration'):
                    result_folder = 'ocr' if job['mode'] == 'ocr' else 'narration'
                    for path in sorted((directory / result_folder).glob('*/*')):
                        if path.suffix in ('.txt', '.json'):
                            archive.write(path, path.relative_to(directory))
            (directory / 'results.zip.tmp').replace(directory / 'results.zip')
            job['status'] = 'completed'
            self.save(job)
        except Exception as error:
            job['status'] = 'failed'
            job['error'] = {'phase': job['phase'], 'page': job.get('position', {}).get('page'),
                            'book': job.get('position', {}).get('book'), 'segment': None,
                            'chunk': job.get('position', {}).get('chunk'),
                            'message': str(error)}
            self.save(job)

    def artifact(self, job_id, name):
        job = self.get(job_id)
        allowed = [item['path'] for item in job['artifacts']]
        if job['status'] == 'completed':
            allowed.append('results.zip')
        if name not in allowed:
            raise FileNotFoundError(name)
        path = self.directory(job_id) / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def page_result(self, job_id, book, page):
        job = self.get(job_id)
        if not 1 <= book <= len(job['books']) or not 1 <= page <= (job['books'][book - 1]['pages'] or 0):
            raise FileNotFoundError('page')
        folder = self.directory(job_id) / 'ocr' / f'{book:03d}'
        result = json.loads((folder / f'page_{page:04d}.json').read_text(encoding='utf-8'))
        result['text'] = (folder / f'page_{page:04d}.txt').read_text(encoding='utf-8')
        return result

    def start_narration(self, parent_id, settings):
        with self.lock:
            settings.validate()
            parent = self.get(parent_id)
            if parent.get('mode') != 'ocr' or parent['status'] != 'completed':
                raise ValueError('完了したOCRジョブを選択してください。')
            job_id = uuid.uuid4().hex
            self.directory(job_id).mkdir()
            job = {'id':job_id, 'created_at':datetime.now(timezone.utc).isoformat(),
                   'status':'queued', 'phase':'llm', 'mode':'narration', 'ocr_parent':parent_id,
                   'books':parent['books'], 'total':parent['total'], 'completed':0,
                   'llm_settings':settings.to_dict(), 'llm_completed':0,
                   'llm_total':sum(len(book_pages(b)) for b in parent['books']), 'review_count':0,
                   'artifacts':[], 'error':None}
            self.save(job)
            self.executor.submit(self.run, job_id)
            return job

    def narration_page(self, job_id, book, page):
        job = self.get(job_id)
        if job.get('mode') != 'narration' or not 1 <= book <= len(job['books']) or page not in book_pages(job['books'][book - 1]):
            raise FileNotFoundError('page')
        path = self.directory(job_id) / 'narration' / f'{book:03d}' / f'page_{page:04d}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def reprocess_ocr(self, parent_id, direction, settings):
        with self.lock:
            parent = self.get(parent_id)
            if parent.get('mode') not in ('ocr', 'receipt') or parent['status'] in ('queued','running'):
                raise ValueError('処理中ではないPDFジョブを選択してください。')
            with ExitStack() as stack:
                sources = [(b['name'], stack.enter_context((self.directory(parent_id)/b['source']).open('rb'))) for b in parent['books']]
                return self.create(sources, direction, 'ocr', settings)


    def delete(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job['status'] not in ('completed', 'failed', 'interrupted'):
                raise ValueError('待機中・処理中のジョブは削除できません。')
            children = [j for j in self.list() if j.get('ocr_parent') == job_id]
            if children:
                raise ValueError('このOCRを参照する朗読変換ジョブがあります。先に該当する朗読変換ジョブを削除してください。')
            shutil.rmtree(self.directory(job_id))
            return {'deleted': job_id}
