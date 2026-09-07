"""Conservative layout cleanup; never correct spelling or infer missing words."""
import re
import unicodedata
from difflib import SequenceMatcher

CLEANUP_VERSION = 'cleanup-v1'
HORIZONTAL = ' \t\u3000'
JAPANESE_SYMBOLS = frozenset('、。！？「」『』（）【】〈〉《》〔〕［］｛｝・ー―…')
OPENING = frozenset('「『（【〈《〔［｛')


def japanese_character(char):
    name = unicodedata.name(char, '')
    return (char in JAPANESE_SYMBOLS or char in '々〆〇ゝゞヽヾ'
            or any(part in name for part in ('HIRAGANA', 'KATAKANA', 'CJK UNIFIED IDEOGRAPH',
                                             'CJK COMPATIBILITY IDEOGRAPH'))
            or 'VARIATION SELECTOR' in name)


def normalize_ocr_text(text):
    """Return clean text plus page statistics. Only explicit horizontal spaces change.

    Join a single line break only after a small set of kana continuation endings,
    before Japanese text, and never before opening dialogue/paragraph delimiters.
    Headings, Latin text, sentence endings and uncertain boundaries remain intact.
    removed_linebreaks counts joined wraps; collapsed_blank_lines counts excess LF.
    CRLF/CR conversion is reported separately from these counts.
    """
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for line in normalized.split('\n'):
        line = re.sub('[ \t\u3000]+', ' ', line.strip(HORIZONTAL))
        chars = []
        for i, char in enumerate(line):
            if (char == ' ' and 0 < i < len(line)-1
                    and japanese_character(line[i-1]) and japanese_character(line[i+1])):
                continue
            chars.append(char)
        lines.append(''.join(chars))
    clean = '\n'.join(lines)
    collapsed = 0
    def collapse(match):
        nonlocal collapsed
        collapsed += len(match[0])-2
        return '\n\n'
    clean = re.sub('\n{3,}', collapse, clean)
    lines = clean.split('\n')
    joined = 0
    parts = [lines[0]]
    for previous, current in zip(lines, lines[1:]):
        wrap = (previous and current and previous[-1] in 'をにがはのでてとへも'
                and japanese_character(current[0]) and current[0] not in JAPANESE_SYMBOLS
                and not any(char in OPENING for char in previous))
        if wrap:
            joined += 1
            parts.append(current)
        else:
            parts.append('\n' + current)
    clean = ''.join(parts)
    changed = sum(max(b-a, d-c) for tag,a,b,c,d in
                  SequenceMatcher(None,text,clean,autojunk=False).get_opcodes() if tag != 'equal')
    return {'text':clean, 'stats':{
        'cleanup_version':CLEANUP_VERSION, 'raw_chars':len(text), 'clean_chars':len(clean),
        'removed_spaces':sum(c in HORIZONTAL for c in text)-sum(c in HORIZONTAL for c in clean),
        'removed_linebreaks':joined, 'collapsed_blank_lines':collapsed,
        'normalized_line_endings':text.count('\r'), 'cleanup_changed_chars':changed}}
