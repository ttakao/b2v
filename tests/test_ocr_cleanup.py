import json
import pytest
from b2v.ocr_cleanup import normalize_ocr_text
from b2v.narration import LLMSettings
from b2v.narration_workflow import process_narration


@pytest.mark.parametrize('raw,expected', [
    ('彼 は 静 か に 歩 い た 。', '彼は静かに歩いた。'),
    ('彼は歩いた 。', '彼は歩いた。'),
    ('「 彼は歩いた。 」', '「彼は歩いた。」'),
    ('IBM PC\nMac mini\nWindows 11\nQwen3 14B\nNew York',
     'IBM PC\nMac mini\nWindows 11\nQwen3 14B\nNew York'),
    ('彼は静かに扉を\n開けた。', '彼は静かに扉を開けた。'),
    ('風が吹いて\nいた。', '風が吹いていた。'),
    ('「そうなのか」\n「そうです」', '「そうなのか」\n「そうです」'),
    ('彼は歩いた。\n\n翌朝になった。', '彼は歩いた。\n\n翌朝になった。'),
    ('彼はたたずんだーーー\n―――\n……\n・・・', '彼はたたずんだーーー\n―――\n……\n・・・'),
    ('誰もいなかつた。\n彼は静かにを開けた。', '誰もいなかつた。\n彼は静かにを開けた。'),
    ('𠮷 野 ヶ 丘 々 𰀀 。', '𠮷野ヶ丘々𰀀。'),
    ('\t 彼 は \u3000\r\n \t\r次。\r', '彼は\n\n次。\n'),
    ('Mac\t\u3000 mini\n\n\n\n次。', 'Mac mini\n\n次。'),
    ('第1章\n目覚め\n「会話は\nまだ続く」', '第1章\n目覚め\n「会話は\nまだ続く」'),
    ('扉を\n「開けて」', '扉を\n「開けて」'),
    ('終わり！\n次？\n声』\n次。', '終わり！\n次？\n声』\n次。'),
    ('英語 IBM PC を使う。', '英語 IBM PC を使う。'),
    ('', ''),
])
def test_conservative_cleanup(raw, expected):
    result = normalize_ocr_text(raw)
    assert result['text'] == expected
    assert result['stats']['raw_chars'] == len(raw)
    assert result['stats']['clean_chars'] == len(expected)
    assert normalize_ocr_text(expected)['text'] == expected


def test_statistics_count_rules_separately():
    result = normalize_ocr_text(' 風 が吹いて\r\nいた。 \n\n\n次。')
    assert result['text'] == '風が吹いていた。\n\n次。'
    stats = result['stats']
    assert stats['removed_spaces'] == 3
    assert stats['removed_linebreaks'] == 1
    assert stats['collapsed_blank_lines'] == 1
    assert stats['normalized_line_endings'] == 1
    assert stats['cleanup_changed_chars'] > 0


def test_workflow_raw_clean_narration_and_clean_context(tmp_path):
    raw_folder = tmp_path/'raw/ocr/001'
    raw_folder.mkdir(parents=True)
    sources = ['前 の 文。\r\n', '彼 は 静 か に扉を\r\n開けた。誰もいなかつた。', '次 の 文。']
    for page, raw in enumerate(sources, 1):
        (raw_folder/f'page_{page:04d}.txt').write_bytes(raw.encode())
    class Provider:
        seen = []
        def request_edits(self, current, previous, following):
            self.seen.append((current,previous,following))
            return {'edits':[dict(original='いなかつた',replacement='いなかった',
                       before_context='',after_context='',reason='OCR誤認識')] if 'いなかつた' in current else []}
    provider = Provider()
    job = {'books':[{'pages':3,'name':'本'}], 'llm_settings':LLMSettings().to_dict(), 'artifacts':[]}
    output = tmp_path/'output'
    process_narration(job,output,tmp_path/'raw',lambda job:None,provider)
    current,previous,following = provider.seen[1]
    assert current == '彼は静かに扉を開けた。誰もいなかつた。'
    assert previous == '前の文。\n' and following == '次の文。'
    folder = output/'narration/001'
    assert (folder/'page_0002_raw.txt').read_bytes() == sources[1].encode()
    assert (raw_folder/'page_0002.txt').read_bytes() == sources[1].encode()
    assert (folder/'page_0002_clean.txt').read_text() == current
    page = json.loads((folder/'page_0002.json').read_text())
    assert page['raw'] == sources[1] and page['clean'] == page['source'] == current
    assert page['narration'] == current.replace('いなかつた','いなかった')
    assert page['chunks'][0]['source'] == current
    assert page['cleanup_stats']['removed_linebreaks'] == 1
    assert 'ocr_cleanup' in (output/'llm_requests.jsonl').read_text()
