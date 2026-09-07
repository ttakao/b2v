"""HTTPに依存しないPhase 1のPDF受付検証とレポート生成。"""
from pathlib import Path
from pypdf import PdfReader


def inspect_pdf(path: Path) -> int:
    with path.open('rb') as stream:
        if not stream.read(1024).lstrip().startswith(b'%PDF-'):
            raise ValueError('PDF形式を確認できません。')
        stream.seek(0)
        reader = PdfReader(stream)
        if reader.is_encrypted:
            raise ValueError('暗号化されたPDFにはまだ対応していません。')
        count = len(reader.pages)
        if not count:
            raise ValueError('ページのないPDFです。')
        return count


def receipt(name: str, pages: int, direction: str) -> str:
    return (f'PDF受付レポート（Phase 1）\n\nファイル: {name}\nページ数: {pages}\n'
            f'基本方向: {direction}\n\nPDFの保存と形式検証が完了しました。\n'
            'OCR・LLM・TTSは未実装です。このファイルは抽出本文ではありません。\n')
