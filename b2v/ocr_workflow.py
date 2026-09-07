"""OCRのページ単位チェックポイントと成果物組み立て。"""
import json
from pathlib import Path
import pymupdf
from .core import inspect_pdf
from .page_selection import selected_pages, parse_pages, book_pages
from .ocr import OCRSettings, TesseractOCR, render_page, detect_direction, effective_direction


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)


def process_ocr(job, directory, save, provider=None):
    provider = provider or TesseractOCR()
    settings = OCRSettings(**job['ocr_settings']).validate()
    job['phase'] = 'ocr'
    job['position'] = {'book': None, 'page': None}
    save(job)
    if hasattr(provider, 'check'):
        provider.check()
    # Validate each source before page processing to get a truthful page total.
    for index, book in enumerate(job['books'], 1):
        job['position'] = {'book': index, 'page': None}
        if book['pages'] is None:
            book['pages'] = inspect_pdf(directory / book['source'])
        book.setdefault('ocr_completed', 0)
        book['selected_pages'] = selected_pages(settings, book['pages'])
        for selection in (settings.horizontal_pages, settings.vertical_pages):
            if selection.strip():
                parse_pages(selection, book['pages'])
    job['ocr_total'] = sum(len(book_pages(book)) for book in job['books'])
    job['ocr_completed'] = 0
    save(job)
    for index, book in enumerate(job['books'], 1):
        folder = directory / 'ocr' / f'{index:03d}'
        folder.mkdir(parents=True, exist_ok=True)
        book['status'] = 'running'
        book['ocr_completed'] = 0
        with pymupdf.open(directory / book['source']) as document:
            for number in book_pages(book):
                job['position'] = {'book': index, 'page': number}
                save(job)
                txt = folder / f'page_{number:04d}.txt'
                meta = folder / f'page_{number:04d}.json'
                if not (txt.is_file() and meta.is_file()):
                    image = render_page(document[number - 1], settings)
                    detection = detect_direction(image)
                    direction = effective_direction(detection, job['direction'], settings.threshold)
                    forced = 'horizontal' if number in parse_pages(settings.horizontal_pages) else ('vertical' if number in parse_pages(settings.vertical_pages) else None)
                    direction_source = 'manual' if forced else ('automatic' if detection['direction'] != 'unknown' and detection['confidence'] >= settings.threshold else 'default')
                    direction = forced or direction
                    blocks = []
                    if direction == 'horizontal' and settings.horizontal_mode == 'blocks' and hasattr(provider, 'recognize_blocks'):
                        text, blocks = provider.recognize_blocks(image, settings.dpi)
                    else:
                        text = provider.recognize(image, direction, settings.dpi)
                    del image
                    atomic_text(txt, text)
                    atomic_text(meta, json.dumps({'page': number, 'detected': detection,
                               'direction': direction, 'direction_source': direction_source, 'blocks': blocks,
                               'horizontal_mode': settings.horizontal_mode, 'characters': len(text.strip()),
                               'empty': not bool(text.strip())}, ensure_ascii=False, indent=2))
                book['ocr_completed'] += 1
                job['ocr_completed'] += 1
                save(job)
        relative = f'results/{index:03d}_raw_ocr.txt'
        target = directory / relative
        target.parent.mkdir(exist_ok=True)
        # Stream the assembled book; do not hold the entire book in memory.
        temp = target.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as output:
            for number in book_pages(book):
                output.write((folder / f'page_{number:04d}.txt').read_text(encoding='utf-8'))
                output.write('\n\f\n')
        temp.replace(target)
        if not any(a['path'] == relative for a in job['artifacts']):
            job['artifacts'].append({'path': relative, 'label': f"{book['name']} — OCR全文TXT"})
        book['status'] = 'completed'
        job['completed'] = sum(b['status'] == 'completed' for b in job['books'])
        save(job)
