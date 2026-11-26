"""Readable authoring preserves exact decisions and publication safety."""

import copy
import json
from types import SimpleNamespace

import pytest

from chef.cli import question_commands as cli
from chef.questions import readability, records
from chef.questions.check import check
from tests.integration.mission.test_question_authoring import draft


def codes(record):
    return {note['code'] for note in readability.analyze(record)}


def test_long_prose_is_advisory_and_preview_patch_stdout_remain_parseable(tmp_path, capsys):
    record = draft()
    record['intro'] = 'A deliberately detailed explanation ' * 12
    source = tmp_path / 'draft.json'
    source.write_text(json.dumps(record), encoding='utf-8')
    changes = tmp_path / 'changes.json'
    changes.write_text('{"set": {"revision": 2}}', encoding='utf-8')
    before = copy.deepcopy(record)
    assert 'long_item' in codes(record)
    assert records.validate_document(record) == before
    assert cli.cmd_question_preview(None, SimpleNamespace(input=source))[0].startswith('# ')
    assert 'long_item' in capsys.readouterr().err
    result = cli.cmd_question_patch(None, SimpleNamespace(input=source, changes=changes, editorial=False))
    assert json.loads(result[0])['revision'] == 2
    assert 'long_sentence' in capsys.readouterr().err
    assert record == before
    assert records.preflight_document(tmp_path, record) == record
    records.publish_document(tmp_path, record)
    result = check(tmp_path / 'confirm-A2.md', tmp_path / 'questions/confirm-A2.json')
    assert not result['issues'] and result['readability']


def test_condition_array_is_included_in_the_entry_measurement():
    record = draft()
    entry = record['blocks'][0]['items'][0]
    entry['conditions'] = {'mode': 'all', 'items': [f'Condition {i}: ' + 'x' * 140 for i in range(12)]}
    records.validate_document(record)
    notes = readability.analyze(record)
    assert any(n['code'] == 'long_item' and n['location'] == 'blocks.rules.unique' and n['characters'] > 1600 for n in notes)


@pytest.mark.parametrize('split', [False, True])
def test_short_items_cannot_hide_document_density_by_splitting_blocks(split):
    record = draft()
    entries = [{'id': f'n{i}', 'label': f'Note {i}', 'text': f'Explanation {i} uses its own saved value and performs the documented action.'} for i in range(100)]
    if split:
        record['blocks'] = [{'id': f'b{i}', 'title': f'Part {i}', 'type': 'list', 'items': [entry]} for i, entry in enumerate(entries)]
    else:
        record['blocks'] = [{'id': 'notes', 'title': 'Notes', 'type': 'list', 'items': entries}]
    records.validate_document(record)
    assert 'dense_document' in codes(record)
    assert ('dense_list' in codes(record)) == (not split)


def test_large_fields_code_and_technical_spans_do_not_trigger_prose_limits():
    record = draft()
    record['blocks'] = [
        {'id': 'fields', 'type': 'fields', 'title': 'Fields', 'fields': [{'name': f'Field{i}', 'type': 'int', 'description': f'Saved value {i}.'} for i in range(100)]},
        {'id': 'code', 'type': 'literal', 'title': 'Code', 'format': 'code', 'value': 'value = 1\n' * 600},
    ]
    assert not codes(record)
    assert not readability.analyze_markdown('````text\n' + 'text ' * 1000 + '\n````\n| Field | Type |\n| A | int |')
    assert not readability.analyze_markdown('Use `' + 'namespace.' * 100 + 'Type` here. Visit https://example.org/' + 'path/' * 100)


def test_prose_in_a_table_cell_is_still_diagnosed():
    record = draft()
    record['blocks'][-1]['rows'][0][0] = 'A long explanation ' * 40
    assert 'long_item' in codes(record)


def test_exact_duplicates_keep_numbers_negation_and_identifier_distinctions():
    common = 'The saved selection remains available after the current document is closed.'
    text = f'## First\n\n{common}\n\n## Second\n\n{common}\n\nThe amount is 100 and the result must not be applied.\n\nThe amount is 200 and the result must be applied.'
    notes = readability.analyze_markdown(text)
    repeated = [n for n in notes if n['code'] == 'duplicate_sentence']
    assert len(repeated) == 1 and len(repeated[0]['locations']) == 2
    assert repeated[0]['text'] == common
    assert not any(n['code'] == 'duplicate_sentence' for n in readability.analyze_markdown('The `' + 'FirstType' + '` value must remain available after saving.\n\nThe `SecondType` value must remain available after saving.'))


def test_paragraph_is_joined_without_bullets_or_required_heading_and_keeps_anchor():
    record = draft()
    record['blocks'].insert(0, {'id': 'context', 'type': 'paragraph', 'sentences': ['First sentence.', 'Second sentence.']})
    text = records.render_document(record)
    assert '<a id="context"></a>\n\nFirst sentence. Second sentence.' in text
    assert '- First sentence' not in text
    broken = copy.deepcopy(record)
    broken['blocks'][0]['sentences'][0] = '<script>bad</script>'
    with pytest.raises(records.QuestionRecordError):
        records.validate_document(broken)


def test_opt_in_decision_view_keeps_answers_in_history_and_current_contract_inline(tmp_path):
    record = draft()
    records.publish_document(tmp_path, record)
    answer = {'schema': 1, 'id': 'reply', 'revision': 1, 'at': '2041-02-08', 'text': 'Use the account option.', 'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the account option.', 'selected': 'account'}], 'approval': {'status': 'complete', 'reason': 'Approved.'}}
    current = records.answer_document(tmp_path, 'confirm-A2.md', answer)
    assert '## 현재 선택' in records.render_document(current)
    changed = records.patch_document(current, {'set': {'revision': 2, 'presentation': 'decision'}}, editorial=True)
    records.publish_document(tmp_path, changed, 1, editorial=True)
    text = (tmp_path / 'confirm-A2.md').read_text(encoding='utf-8')
    assert '## 현재 선택' not in text and 'Stable ID.' in text
    assert '<details' not in text and '## 결정할 것' not in text
    assert changed['items'] == current['items'] and changed['answers'] == current['answers']
    history = (tmp_path / 'details/history/confirm-A2.md').read_text(encoding='utf-8')
    assert 'Use the account option.' in history and 'All devices use it.' in history
    assert not check(tmp_path / 'confirm-A2.md', tmp_path / 'questions/confirm-A2.json')['issues']


@pytest.mark.parametrize('schema', [1, 2])
def test_decision_options_have_no_mechanical_labels_or_forced_line_breaks(schema):
    record = draft()
    record['schema'] = schema
    if schema == 1:
        record['blocks'] = [{'id': 'snapshot', 'type': 'paragraph', 'text': 'Current complete contract.'}]
    record['presentation'] = 'decision'
    text = records.render_document(record)
    assert '**Account** — Share state. All devices use it.' in text
    assert '설명 ·' not in text and '영향 ·' not in text


def test_decision_view_cannot_be_enabled_without_current_blocks():
    record = draft()
    record['presentation'] = 'decision'
    del record['blocks']
    with pytest.raises(records.QuestionRecordError, match='전체 설계'):
        records.validate_document(record)


def test_readability_excludes_raw_answers_and_user_approval_reason(tmp_path):
    record = draft()
    raw = 'Keep this answer verbatim. ' * 100
    records.publish_document(tmp_path, record)
    saved = records.answer_document(tmp_path, 'confirm-A2.md', {'schema': 1, 'id': 'reply', 'revision': 1, 'at': '2041-02-08', 'text': raw, 'updates': [{'id': 'storage', 'status': 'answered', 'answer': raw, 'selected': 'account'}], 'approval': {'status': 'pending', 'reason': raw}})
    assert not readability.analyze(saved)


def test_independent_conditions_have_separate_sentence_boundaries():
    record = draft()
    record['blocks'][0]['items'][0]['conditions'] = {'mode': 'all', 'items': ['a' * 98, 'b' * 98, 'c' * 98]}
    notes = readability.analyze(record)
    assert not any(n['code'] == 'long_sentence' for n in notes)


def test_escaped_backticks_are_prose_and_url_sentence_end_is_preserved():
    assert 'long_sentence' in {n['code'] for n in readability.analyze_markdown('Use \\`' + 'ordinary prose ' * 45 + '\\` literally.')}
    assert not readability.analyze_markdown('a' * 100 + ' https://example.org. ' + 'b' * 100 + '.')


def test_raw_markdown_table_cells_are_measured_without_row_density():
    notes = readability.analyze_markdown('| Field | Meaning |\n| --- | --- |\n| A | ' + 'A long explanation ' * 40 + '|')
    assert any(n['code'] == 'long_item' and ':C2' in n['location'] for n in notes)
    assert not any(n['code'].startswith('dense_') for n in notes)


def test_main_and_generated_detail_density_are_separate():
    record = draft()
    entries = [{'id': f'n{i}', 'label': f'Note {i}', 'text': f'Explanation {i} ' + 'x' * 90} for i in range(25)]
    record['blocks'] = [{'id': 'main', 'title': 'Main', 'type': 'list', 'items': entries}]
    record['detail_blocks'] = [{'id': 'detail', 'title': 'Detail', 'type': 'list', 'items': copy.deepcopy(entries)}]
    assert 'dense_document' not in codes(record)


@pytest.mark.parametrize('value', ['---', '* * *', '___', '    indented code'])
def test_new_paragraph_cannot_inject_block_markdown(value):
    record = draft()
    record['blocks'].append({'id': 'paragraph', 'type': 'paragraph', 'sentences': [value]})
    with pytest.raises(records.QuestionRecordError, match='블록 Markdown'):
        records.validate_document(record)


@pytest.mark.parametrize('ending', ['', '|'])
@pytest.mark.parametrize('leading', ['', '|'])
def test_markdown_table_prose_is_measured_with_optional_edge_pipes(ending, leading):
    text = f'{leading} A | B {ending}\n{leading} --- | --- {ending}\n{leading} A | ' + 'A long explanation ' * 40 + ending
    assert any(n['code'] == 'long_item' for n in readability.analyze_markdown(text))


def test_closing_code_delimiter_does_not_use_backslash_escaping_inside_span():
    text = 'Use `path\\` then ' + 'ordinary prose ' * 45 + ' `tag` end.'
    assert any(n['code'] == 'long_sentence' for n in readability.analyze_markdown(text))


def test_joined_paragraph_cannot_compose_block_markup():
    record = draft()
    record['blocks'].append({'id': 'paragraph', 'type': 'paragraph', 'sentences': ['-', '-', '-']})
    with pytest.raises(records.QuestionRecordError, match='블록 Markdown'):
        records.validate_document(record)


@pytest.mark.parametrize('slashes,column', [(1, 'C1'), (2, 'C2'), (3, 'C1'), (4, 'C2')])
def test_markdown_table_pipe_escape_uses_backslash_parity(slashes, column):
    text = '| val' + '\\' * slashes + '| ' + 'A long explanation ' * 40 + '|'
    notes = readability.analyze_markdown(text)
    assert any(n['code'] == 'long_item' and n['location'].endswith(column) for n in notes)
