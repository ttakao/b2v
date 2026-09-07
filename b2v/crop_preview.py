"""PDF previews only; no OCR is invoked here."""
import base64
import pymupdf
import cv2
from .ocr import OCRSettings, render_page


def preview_page(path, number):
    if not path.is_file():
        raise FileNotFoundError(str(path))
    try:
        document = pymupdf.open(path)
    except pymupdf.FileDataError as exc:
        raise ValueError('PDFを画像化できません。ファイルを確認してください。') from exc
    with document:
        if document.needs_pass or not len(document):
            raise ValueError('パスワード付き、またはページのないPDFは使用できません。')
        if not 1 <= number <= len(document):
            raise ValueError('PDFページ番号が範囲外です。')
        page = document[number-1]
        dpi = max(1, min(100, int(1600 * 72 / max(page.rect.width, page.rect.height))))
        image = render_page(page, OCRSettings(dpi=dpi, top=0, bottom=0, left=0, right=0))
        ok, png = cv2.imencode('.png', image)
        if not ok:
            raise ValueError('プレビュー画像を生成できませんでした。')
        height, width = image.shape
        return {'page':number, 'pages':len(document), 'width':width, 'height':height,
                'image':'data:image/png;base64,'+base64.b64encode(png).decode('ascii')}
