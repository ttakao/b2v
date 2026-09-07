"""ページ処理と差し替え可能なOCR Provider。HTTPには依存しない。"""
import os
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol
import cv2
import numpy as np
import pymupdf
from .page_selection import parse_pages


@dataclass(frozen=True)
class OCRSettings:
    dpi: int = 300
    top: float = 3
    bottom: float = 3
    left: float = 3
    right: float = 3
    threshold: float = .75
    pages: str = ''
    exclude_pages: str = ''
    horizontal_pages: str = ''
    vertical_pages: str = ''
    horizontal_mode: str = 'blocks'


    def validate(self):
        if not 100 <= self.dpi <= 400:
            raise ValueError('解像度は100〜400dpiにしてください。')
        if not all(np.isfinite(v) and 0 <= v <= 30 for v in (self.top, self.bottom, self.left, self.right)):
            raise ValueError('cropは各辺0〜30%にしてください。')
        if not np.isfinite(self.threshold) or not .5 <= self.threshold <= 1:
            raise ValueError('自動判定閾値は0.5〜1にしてください。')
        for selection in (self.pages, self.exclude_pages, self.horizontal_pages, self.vertical_pages):
            if len(selection) > 2000:
                raise ValueError('ページ指定が長すぎます。')
            parse_pages(selection)
        if set(parse_pages(self.horizontal_pages)) & set(parse_pages(self.vertical_pages)):
            raise ValueError('横書き固定と縦書き固定のページが重複しています。')
        if self.horizontal_mode not in ('blocks', 'page'):
            raise ValueError('横書きOCR方式が不正です。')
        return self

    def to_dict(self):
        return asdict(self)


class OCRProvider(Protocol):
    def recognize(self, image: np.ndarray, direction: str, dpi: int) -> str: ...


class TesseractOCR:
    def __init__(self):
        self.binary = shutil.which('tesseract') or '/opt/homebrew/bin/tesseract'

    def check(self):
        try:
            result = subprocess.run([self.binary, '--list-langs'], capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Tesseractが利用できません。brew install tesseract tesseract-lang を実行してください。') from exc
        missing = {'jpn', 'jpn_vert'} - set(result.stdout.splitlines())
        if missing:
            raise RuntimeError('日本語OCRデータが不足しています: ' + ', '.join(sorted(missing)))
        return {'ready': True, 'engine': 'Tesseract', 'languages': ['jpn', 'jpn_vert']}

    def recognize(self, image, direction, dpi):
        lang, psm = ('jpn_vert', '5') if direction == 'vertical' else ('jpn', '3')
        return self._recognize(image, lang, psm, dpi)

    def recognize_blocks(self, image, dpi):
        boxes = horizontal_blocks(image)
        if not boxes:
            return '', []
        text = []
        for x, y, width, height in boxes:
            # Give Tesseract a white border around each line, avoiding page-layout guesses.
            block = cv2.copyMakeBorder(image[y:y+height, x:x+width], 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
            text.append(self._recognize(block, 'jpn', '7', dpi).rstrip('\n'))
        return '\n'.join(text) + '\n', boxes

    def _recognize(self, image, lang, psm, dpi):
        ok, png = cv2.imencode('.png', image)
        if not ok:
            raise RuntimeError('OCR画像のエンコードに失敗しました。')
        result = subprocess.run([self.binary, 'stdin', 'stdout', '-l', lang, '--psm', psm, '--dpi', str(dpi)],
                                input=png.tobytes(), capture_output=True, timeout=180,
                                env={**os.environ, 'OMP_THREAD_LIMIT': '2'})
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-2000:])
        return result.stdout.decode('utf-8')


def crop_image(image, settings):
    height, width = image.shape[:2]
    x0, x1 = round(width * settings.left / 100), round(width * (1 - settings.right / 100))
    y0, y1 = round(height * settings.top / 100), round(height * (1 - settings.bottom / 100))
    if x1 <= x0 or y1 <= y0:
        raise ValueError('crop後の画像が空です。')
    return image[y0:y1, x0:x1].copy()


def _bands(profile, minimum_width=2):
    # Measure interior whitespace bands: number, width, and spacing regularity.
    active = np.flatnonzero(profile > max(.003, float(profile.max()) * .12))
    if len(active) < 8:
        return 0.
    profile = profile[active[0]:active[-1] + 1]
    spaces = profile < max(.003, float(profile.max()) * .12)
    edges = np.diff(np.r_[False, spaces, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    widths = ends - starts
    keep = widths >= max(minimum_width, len(profile) * .003)
    centers = (starts[keep] + ends[keep]) / 2
    widths = widths[keep]
    if len(centers) < 2:
        return 0.
    distances = np.diff(centers)
    regularity = 1 / (1 + float(np.std(distances) / max(1, np.mean(distances))))
    return float(min(len(centers) / 8, 1) * regularity * np.mean(widths) / max(1, np.mean(distances)))


def layout_geometry(image):
    _, ink = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    components = stats[1:]
    components = components[components[:, 4] > 15]
    if not len(components):
        return 0., [], []
    size = max(3., float(np.percentile(np.maximum(components[:, 2], components[:, 3]), 65)))
    results = []
    for horizontal in (True, False):
        long, short = max(3, int(size * .9)), max(1, int(size * .12))
        kernel = (long, short) if horizontal else (short, long)
        connected = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, kernel))
        _, _, regions, _ = cv2.connectedComponentsWithStats(connected)
        results.append([list(map(int, r[:4])) for r in regions[1:]
                        if (r[2] / r[3] if horizontal else r[3] / r[2]) >= 3
                        and max(r[2:4]) > size * 3 and min(r[2:4]) > size * .3])
    return size, results[0], results[1]


def horizontal_blocks(image):
    size, _, _ = layout_geometry(image)
    if not size:
        return []
    _, ink = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    components = [list(map(int, r[:4])) for r in stats[1:]
                  if r[4] >= max(8, size * size * .025) and max(r[2:4]) >= size * .25]
    lines = []
    for x, y, width, height in sorted(components, key=lambda r: (r[1] + r[3] / 2, r[0])):
        center = y + height / 2
        match = next((line for line in reversed(lines) if abs(center - (line[1]+line[3])/2) <= max(height, line[3]-line[1]) * .6), None)
        if match is None:
            lines.append([x, y, x+width, y+height])
        else:
            match[:] = [min(match[0], x), min(match[1], y), max(match[2], x+width), max(match[3], y+height)]
    # Include punctuation around the baseline. Each region is an explicit horizontal text line.
    h, w = image.shape
    padding = max(3, int(size * .15))
    boxes = []
    for x0, y0, x1, y1 in sorted(lines, key=lambda r: (r[1], r[0])):
        if y1-y0 < size*.25:
            continue
        x0, y0 = max(0, x0-padding), max(0, y0-padding)
        x1, y1 = min(w, x1+padding), min(h, y1+padding)
        boxes.append([x0, y0, x1-x0, y1-y0])
    merged = []
    for x, y, width, height in boxes:
        if merged and y < merged[-1][1]+merged[-1][3]:
            px, py, pw, ph = merged[-1]
            left, right = min(px,x), max(px+pw,x+width)
            merged[-1] = [left, py, right-left, max(py+ph,y+height)-py]
        else:
            merged.append([x,y,width,height])
    return merged


def detect_direction(image):
    size, horizontal, vertical = layout_geometry(image)
    h_score = sum(w*h for x,y,w,h in horizontal)
    v_score = sum(w*h for x,y,w,h in vertical)
    if size and h_score + v_score:
        confidence = abs(h_score-v_score) / (h_score+v_score)
        support = min(1., max(h_score,v_score) / (size*size*8))
        if confidence * support >= .75:
            return {'direction':'horizontal' if h_score > v_score else 'vertical',
                    'confidence':round(confidence*support,3), 'method':'line_geometry'}
    _, ink = cv2.threshold(image, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Sparse/blank pages and solid pictures should not force an override.
    fraction = float(ink.mean())
    if fraction < .002 or fraction > .45:
        return {'direction': 'unknown', 'confidence': 0.0}
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    components = stats[1:]
    components = components[components[:, cv2.CC_STAT_AREA] >= 8]
    if len(components) < 3:
        return {'direction': 'unknown', 'confidence': 0.0}
    # Ignore intra-character and narrow inter-character gaps. Use the smaller
    # component dimension so connected horizontal/vertical strokes remain usable.
    typical_size = float(np.median(np.minimum(components[:, 2], components[:, 3])))
    gap_width = max(2, typical_size * .7)
    x, y = _bands(ink.mean(axis=0), gap_width), _bands(ink.mean(axis=1), gap_width)
    confidence = abs(x - y) / max(x + y, 1e-9)
    if max(x, y) < .015 or confidence < .2:
        return {'direction': 'unknown', 'confidence': round(confidence, 3)}
    return {'direction': 'vertical' if x > y else 'horizontal', 'confidence': round(confidence, 3)}


def effective_direction(detection, default, threshold):
    if detection['direction'] != 'unknown' and detection['confidence'] >= threshold:
        return detection['direction']
    return default if default in ('vertical', 'horizontal') else 'vertical'


def render_page(page, settings):
    # Refuse giant canvases before allocation. Only this page is resident.
    pixels = page.rect.width * page.rect.height * (settings.dpi / 72) ** 2
    if pixels > 40_000_000:
        raise ValueError('ページ画像が4000万画素を超えます。dpiを下げて再登録してください。')
    pix = page.get_pixmap(dpi=settings.dpi, colorspace=pymupdf.csGRAY, alpha=False)
    image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return crop_image(image, settings)
