import json

import httpx
import pytest

from b2v.narration import EDIT_FIELDS, LLMSettings, LlamaNarrator, apply_edits
from b2v.narration_workflow import process_narration


def edit(original, replacement, before='', after='', reason='OCR誤認識'):
    return dict(original=original, replacement=replacement, before_context=before,
                after_context=after, reason=reason)


@pytest.mark.parametrize('source', ['彼は静かに歩いていた。', ' \n𠮷野\t\n\n', ''])
def test_no_changes_preserve_every_character(source):
    result = apply_edits(source, [])
    assert result['source'] == result['narration'] == source
    assert result['changed_chars'] == result['applied_edit_count'] == 0
    assert not result['warnings']


@pytest.mark.parametrize('source,patch,expected,lexical,pause', [
    ('誰もいなかつた。', edit('いなかつた', 'いなかった'), '誰もいなかった。', True, False),
    ('風が吹いて\nいた。', edit('\n', '', '風が吹いて', 'いた。'), '風が吹いていた。', False, False),
    ('彼はたたずんだーーー', edit('ーーー', '……'), '彼はたたずんだ……', False, True),
    ('彼は静かにを開けた。', edit('静かにを開けた', '静かに扉を開けた', '彼は', '。', 'OCR脱字'),
     '彼は静かに扉を開けた。', True, False),
])
def test_allowed_edits(source, patch, expected, lexical, pause):
    result = apply_edits(source, [patch])
    assert result['source'] == source
    assert result['narration'] == expected
    assert result['applied_edit_count'] == 1
    assert result['rejected_edit_count'] == 0
    assert result['lexical_change'] is lexical
    assert result['pause_symbol_change'] is pause
    assert result['changed_chars'] == (3 if pause else 1)
    assert result['change_ratio'] == result['changed_chars'] / len(source)


def test_repeated_text_uses_exact_adjacent_context():
    source = '家にはいなかつた。そこには誰もいなかつた。'
    patch = edit('いなかつた', 'いなかった', 'そこには誰も', '。')
    assert apply_edits(source, [patch])['narration'] == '家にはいなかつた。そこには誰もいなかった。'
    result = apply_edits(source, [edit('いなかつた', 'いなかった')])
    assert result['narration'] == source
    assert result['rejected_edit_count'] == 1


@pytest.mark.parametrize('patch', [edit('いなかった', 'いた'), edit('', '扉'),
    edit('いなかつた', 'いなかった', '誰か'), {'original': 'いなかつた'},
    edit('いなかつた', 'いなかつた'), dict(edit('いなかつた', 'いなかった'), start=0)])
def test_invalid_edits_never_force_a_match(patch):
    source = '誰もいなかつた。'
    result = apply_edits(source, [patch])
    assert result['narration'] == source
    assert result['warnings'] and result['rejected_edit_count'] == 1


def test_all_overlapping_edits_rejected_but_independent_edit_applied():
    result = apply_edits('abcdef。', [edit('abc', 'X'), edit('cd', 'Y'),
                                  edit('def', 'Z'), edit('。', '！')])
    assert result['narration'] == 'abcdef！'
    assert result['rejected_edit_count'] == 3
    assert result['applied_edit_count'] == 1
    assert apply_edits('abc', [edit('b', 'X'), edit('b', 'Y')])['narration'] == 'abc'


def test_spans_resolved_before_expansion_and_adjacent_edits():
    result = apply_edits('𠮷ab終', [edit('a', '長い文字列', '𠮷', 'b終'),
                               edit('b', '', '𠮷a', '終')])
    assert result['narration'] == '𠮷長い文字列終'
    assert result['applied_edit_count'] == 2


def mock_response(monkeypatch, body, check=lambda payload: None):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json):
            check(json)
            return httpx.Response(200, request=httpx.Request('POST', url), json=body)
    monkeypatch.setattr('b2v.narration.httpx.Client', Client)


def test_protocol_full_input_and_per_request_metrics(monkeypatch, caplog):
    source, previous, following = '長い原文。\n' * 200, ' 前文\n', '\n後文 '
    def check(payload):
        assert json.loads(payload['messages'][1]['content']) == dict(current=source, previous=previous, following=following)
        assert payload['max_tokens'] == 2048
        assert payload['chat_template_kwargs']['enable_thinking'] is False
        schema = payload['response_format']['json_schema']
        assert schema['strict'] is True
        assert set(schema['schema']['properties']) == {'edits'}
        assert schema['schema']['properties']['edits']['items']['required'] == list(EDIT_FIELDS)
    mock_response(monkeypatch, {'choices':[{'finish_reason':'stop','message':{'content':'{"edits":[]}'}}],
        'usage':{'prompt_tokens':1000,'completion_tokens':5},
        'timings':{'prompt_ms':123,'predicted_ms':45,'predicted_per_second':30}}, check)
    provider = LlamaNarrator(LLMSettings())
    events = []
    provider.metrics_sink = events.append
    result = provider.request_edits(source, previous, following)
    metrics = result['metrics']
    assert result['edits'] == []
    assert metrics['current_chars'] == len(source)
    assert metrics['previous_chars'] == len(previous)
    assert metrics['following_chars'] == len(following)
    assert metrics['prompt_tokens'] == 1000 and metrics['completion_tokens'] == 5
    assert metrics['generation_time'] == 45 and metrics['prompt_eval_time'] == 123
    assert metrics['tokens_per_second'] == 30 and metrics['total_time'] >= 0
    assert metrics['edit_count'] == 0 and metrics['output_chars'] == 12
    assert events == [metrics]
    assert 'llm_request' in caplog.text


@pytest.mark.parametrize('content,finish', [('{"narration":"書き換え"}', 'stop'), ('{"edits":', 'length')])
def test_no_full_text_fallback_and_failures_logged(monkeypatch, content, finish):
    mock_response(monkeypatch, {'choices':[{'finish_reason':finish,'message':{'content':content}}]})
    provider = LlamaNarrator(LLMSettings())
    events = []
    provider.metrics_sink = events.append
    with pytest.raises(RuntimeError):
        provider.request_edits('原文', '', '')
    assert len(events) == 1 and events[0]['status'] == 'failed'
    assert events[0]['prompt_tokens'] is None and events[0]['generation_time'] is None


def test_workflow_preserves_page_and_records_rejections(tmp_path):
    raw = tmp_path/'ocr/001'
    raw.mkdir(parents=True)
    source = '静かな夜だった。\n' * 90
    (raw/'page_0001.txt').write_text(source)
    class Provider:
        def request_edits(self, current, previous, following):
            self.metrics_sink({'current_chars':len(current)})
            return {'edits':[edit('存在しない', '変更')], 'metrics':{'current_chars':len(current)}}
    job = {'id':'test', 'books':[{'pages':1,'name':'本'}], 'llm_settings':LLMSettings(chunk_chars=200).to_dict(), 'artifacts':[]}
    output = tmp_path/'output'
    process_narration(job, output, tmp_path, lambda job: None, Provider())
    assert (output/'narration/001/page_0001.txt').read_text() == source
    events = [json.loads(line) for line in (output/'llm_requests.jsonl').read_text().splitlines()]
    assert any(e['event'] == 'edits_applied' and e['rejected_edit_count'] == 1 for e in events)
    job.pop('narration_format')
    with pytest.raises(RuntimeError, match='旧方式'):
        process_narration(job, output, tmp_path, lambda job: None, Provider())
