"""PDFの1始まりのページ番号を扱う。印刷されたノンブルとは別。"""
import re


def parse_pages(value, total=None):
    if not value.strip():
        return list(range(1, total + 1)) if total is not None else []
    pages = set()
    for part in value.replace('、', ',').split(','):
        match = re.fullmatch(r'\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*', part)
        if not match:
            raise ValueError('ページ指定は 5-20,23,30-50 の形式にしてください。')
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last < first or last > 100000 or (total is not None and last > total):
            raise ValueError(f'ページ指定が範囲外です（PDFは{total or "最大100000"}ページ）。')
        pages.update(range(first, last + 1))
    return sorted(pages)


def selected_pages(settings, total):
    selected = parse_pages(settings.pages, total)
    excluded = set(parse_pages(settings.exclude_pages, total)) if settings.exclude_pages.strip() else set()
    selected = [p for p in selected if p not in excluded]
    if not selected:
        raise ValueError('処理対象ページがありません。')
    return selected


def book_pages(book):
    return book.get('selected_pages', list(range(1, book['pages'] + 1)))
