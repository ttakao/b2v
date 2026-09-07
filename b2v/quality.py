"""Tesseract recognition statistics and per-book ranking (not reading accuracy)."""
import csv
import io
import math


def parse_tsv(text):
    units = []
    for row in csv.DictReader(io.StringIO(text), delimiter='\t', quoting=csv.QUOTE_NONE):
        try:
            confidence = float(row.get('conf', -1))
            if row.get('level') != '5' or not math.isfinite(confidence) or not 0 <= confidence <= 100 or not row.get('text', '').strip():
                continue
            units.append({'text':row['text'], 'confidence':confidence})
        except (ValueError, TypeError):
            continue
    return units


def statistics(units, low=50, very_low=20):
    if not 0 <= very_low <= low <= 100:
        raise ValueError('confidence閾値は0≦very low≦low≦100で指定してください。')
    count = len(units)
    low_items = [u for u in units if u['confidence'] < low]
    very = sum(u['confidence'] < very_low for u in units)
    return {'mean_ocr_confidence':sum(u['confidence'] for u in units)/count if count else None,
            'recognized_unit_count':count, 'low_confidence_count':len(low_items),
            'low_confidence_ratio':len(low_items)/count if count else None,
            'very_low_confidence_count':very, 'very_low_confidence_ratio':very/count if count else None,
            'low_confidence_items':low_items}


def classify(pages, percentile=10, minimum=20):
    if not math.isfinite(percentile) or not 1 <= percentile <= 100 or minimum < 1:
        raise ValueError('下位割合は1〜100%、最低認識単位数は1以上で指定してください。')
    eligible = sorted((p for p in pages if p['mean_ocr_confidence'] is not None and p['recognized_unit_count'] >= minimum), key=lambda p:p['mean_ocr_confidence'])
    boundary = eligible[math.ceil(len(eligible)*percentile/100)-1]['mean_ocr_confidence'] if eligible else None
    for page in pages:
        score=page['mean_ocr_confidence']
        page['quality'] = 'UNAVAILABLE' if score is None else 'LOW_TEXT' if page['recognized_unit_count'] < minimum else 'REVIEW' if score <= boundary else 'AUTO_OK'
        page['review_rank'] = 1+sum(p['mean_ocr_confidence'] < score for p in eligible) if page['quality'] in ('REVIEW','AUTO_OK') else None
        page['candidate'] = page['quality'] != 'AUTO_OK' or percentile == 100
        page['auto_selected'] = page['quality'] == 'REVIEW'
    return boundary
