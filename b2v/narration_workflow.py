"""OCRとは独立した変換ジョブ。原文と前後文脈・変換結果を保存。"""
import json
from datetime import datetime, timezone
from .ocr_workflow import atomic_text
from .page_selection import book_pages
from .ocr_cleanup import normalize_ocr_text, CLEANUP_VERSION
from .narration import LLMSettings, LlamaNarrator, split_text, apply_edits, fingerprint, FORMAT_VERSION


def process_narration(job, directory, ocr_directory, save, provider=None):
    if job.get('narration_format') != FORMAT_VERSION and any((directory/'narration').glob('*/*chunk*.json')):
        raise RuntimeError('旧方式の変換結果は差分方式へ混在させません。OCR履歴から新しい朗読変換ジョブを作成してください。')
    job['narration_format'] = FORMAT_VERSION
    directory.mkdir(parents=True, exist_ok=True)
    def event_log(event):
        with (directory/'llm_requests.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'timestamp':datetime.now(timezone.utc).isoformat(),
                         'job_id':job.get('id'), **job.get('position',{}), **event}, ensure_ascii=False)+'\n')
    settings = LLMSettings(**job['llm_settings']).validate()
    provider = provider or LlamaNarrator(settings)
    provider.metrics_sink = lambda metrics: event_log({'event':'llm_request', **metrics})
    job['phase'] = 'llm'
    job['llm_completed'] = 0
    job['review_count'] = 0
    job['completed'] = 0
    for book in job['books']:
        book['narration_completed'] = 0
    save(job)
    if hasattr(provider, 'health'):
        provider.health()
    for book_index, book in enumerate(job['books'], 1):
        raw_folder = ocr_directory / 'ocr' / f'{book_index:03d}'
        folder = directory / 'narration' / f'{book_index:03d}'
        folder.mkdir(parents=True, exist_ok=True)
        for page in book_pages(book):
            job['position'] = {'book': book_index, 'page': page, 'chunk': None}
            save(job)
            def read_raw(number):
                with (raw_folder / f'page_{number:04d}.txt').open(encoding='utf-8', newline='') as stream:
                    return stream.read()
            raw = read_raw(page)
            cleanup = normalize_ocr_text(raw)
            source = cleanup['text']
            atomic_text(folder / f'page_{page:04d}_raw.txt', raw)
            atomic_text(folder / f'page_{page:04d}_clean.txt', source)
            atomic_text(folder / f'page_{page:04d}_cleanup.json', json.dumps(cleanup['stats'], ensure_ascii=False, indent=2))
            event_log({'event':'ocr_cleanup', **cleanup['stats']})
            previous_page = normalize_ocr_text(read_raw(page-1))['text'] if page-1 in book_pages(book) else ''
            following_page = normalize_ocr_text(read_raw(page+1))['text'] if page+1 in book_pages(book) else ''
            chunks = split_text(source, settings.chunk_chars)
            results = []
            job['page_chunks'] = len(chunks)
            for chunk_index, current in enumerate(chunks, 1):
                job['position']['chunk'] = chunk_index
                save(job)
                before = ''.join(chunks[:chunk_index-1]) or previous_page
                after = ''.join(chunks[chunk_index:]) or following_page
                previous = before[-settings.context_chars:] if settings.context_chars else ''
                following = after[:settings.context_chars]
                identity = fingerprint({'source':current, 'previous':previous, 'following':following,
                                        'settings':job['llm_settings'], 'format_version':FORMAT_VERSION, 'cleanup_version':CLEANUP_VERSION})
                path = folder / f'page_{page:04d}_chunk_{chunk_index:04d}.json'
                result = json.loads(path.read_text()) if path.exists() else None
                if result is None or result.get('fingerprint') != identity:
                    response = provider.request_edits(current, previous, following)
                    result = {**apply_edits(current, response['edits']), 'previous':previous, 'following':following,
                              'page':page, 'chunk':chunk_index, 'fingerprint':identity,
                              'format_version':FORMAT_VERSION, 'metrics':response.get('metrics')}
                    event_log({'event':'edits_applied', 'applied_edit_count':result['applied_edit_count'],
                               'rejected_edit_count':result['rejected_edit_count'], 'changed_chars':result['changed_chars'],
                               'change_ratio':result['change_ratio'], 'lexical_change':result['lexical_change'],
                               'pause_symbol_change':result['pause_symbol_change'], 'warnings':result['warnings']})
                    atomic_text(path, json.dumps(result, ensure_ascii=False, indent=2))
                results.append(result)
                job['review_count'] += bool(result['warnings'])
            # No inserted separators: untouched source characters remain exactly unchanged.
            narration = ''.join(r['narration'] for r in results)
            atomic_text(folder / f'page_{page:04d}.txt', narration)
            atomic_text(folder / f'page_{page:04d}.json', json.dumps({'page':page, 'source':source,
                        'raw':raw, 'clean':source, 'cleanup_stats':cleanup['stats'],
                        'narration':narration, 'chunks':results}, ensure_ascii=False, indent=2))
            job['llm_completed'] += 1
            book['narration_completed'] += 1
            save(job)
        relative = f'results/{book_index:03d}_narration.txt'
        target = directory / relative
        target.parent.mkdir(exist_ok=True)
        with target.with_suffix('.tmp').open('w', encoding='utf-8') as output:
            for page in book_pages(book):
                output.write((folder / f'page_{page:04d}.txt').read_text(encoding='utf-8'))
                output.write('\n\f\n')
        target.with_suffix('.tmp').replace(target)
        if not any(a['path'] == relative for a in job['artifacts']):
            job['artifacts'].append({'path':relative, 'label':f"{book['name']} — 朗読用TXT"})
        job['completed'] += 1
        save(job)
