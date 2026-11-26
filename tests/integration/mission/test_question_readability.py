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


def test_ask_decision_view_hides_completed_choice_and_preserves_current_result_and_history(tmp_path):
    record = draft()
    record.update(id='ask-reading-format', title='Reading list question', intro='Choose a format for a fictional reading list.')
    record['items'] = [{
        'id': 'format', 'title': 'How should progress be stored?', 'body': 'Choose the position format.',
        'status': 'pending', 'answer': '', 'options': [
            {'id': 'page', 'label': 'Page number', 'description': 'Save a page position.'},
            {'id': 'percent', 'label': 'Percentage', 'description': 'Save a percentage.'},
        ],
    }]
    record['blocks'] = [{
        'id': 'result', 'type': 'paragraph', 'title': 'Current result',
        'sentences': ['The list stores reading progress.'],
    }]
    approval = copy.deepcopy(record['approval'])
    records.publish_document(tmp_path, record)
    answer = {
        'schema': 1, 'id': 'reply', 'revision': 1, 'at': '2041-02-08',
        'text': 'Use page numbers for this list.',
        'updates': [{'id': 'format', 'status': 'answered', 'answer': 'Use page numbers for this list.', 'selected': 'page'}],
    }
    current = records.answer_document(tmp_path, 'ask-reading-format.md', answer)
    changed = records.patch_document(current, {
        'set': {'revision': 2, 'presentation': 'decision'},
        'upsert_blocks': [{
            'id': 'result', 'type': 'paragraph', 'title': 'Current result',
            'sentences': ['The list stores each reading position as a page number.'],
        }],
    }, editorial=True)
    records.publish_document(tmp_path, changed, 1, editorial=True)

    text = (tmp_path / 'ask-reading-format.md').read_text(encoding='utf-8')
    history = (tmp_path / 'details/history/ask-reading-format.md').read_text(encoding='utf-8')
    assert 'The list stores each reading position as a page number.' in text
    assert 'Use page numbers for this list.' not in text
    assert 'Page number' not in text
    assert 'Use page numbers for this list.' in history and '선택 id: `page`' in history
    assert changed['items'][0]['status'] == 'answered' and changed['items'][0]['selected'] == 'page'
    assert changed['answers'] == current['answers'] and changed['approval'] == approval
    assert not check(tmp_path / 'ask-reading-format.md', tmp_path / 'questions/ask-reading-format.json')['issues']


@pytest.mark.parametrize('document_id', ['ask-reading-format', 'confirm-A3'])
def test_new_answer_resets_decision_view_but_replay_preserves_it(tmp_path, document_id):
    record = draft()
    record['id'] = document_id
    record['intro'] = 'Old intro sentinel.'
    record['approval']['reason'] = 'Old approval reason sentinel.'
    record['detail_blocks'] = [{
        'id': 'prior-detail', 'type': 'paragraph', 'title': 'Prior detail',
        'sentences': ['Old detail sentinel.'],
    }]
    record['items'].append({
        'id': 'notes', 'title': 'Include notes?', 'body': 'Choose whether to include notes.',
        'status': 'pending', 'answer': '', 'options': [
            {'id': 'include', 'label': 'Include notes', 'description': 'Keep notes with the list.'},
            {'id': 'omit', 'label': 'Omit notes', 'description': 'Keep only titles.'},
        ],
    })
    record['blocks'] = [{
        'id': 'result', 'type': 'paragraph', 'title': 'Current result',
        'sentences': ['The list stores reading progress.'],
    }]
    approval = copy.deepcopy(record['approval'])
    records.publish_document(tmp_path, record)
    first_answer = {
        'schema': 1, 'id': 'first-reply', 'revision': 1, 'at': '2041-02-08',
        'text': 'Use the account option.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the account option.', 'selected': 'account'}],
    }
    current = records.answer_document(tmp_path, f'{document_id}.md', first_answer)
    decision = records.patch_document(current, {
        'set': {'revision': 2, 'presentation': 'decision'},
        'upsert_blocks': [{
            'id': 'result', 'type': 'paragraph', 'title': 'Current result',
            'sentences': ['The list stores reading progress on the account.'],
        }],
    }, editorial=True)
    records.publish_document(tmp_path, decision, 1, editorial=True)

    next_answer = {
        'schema': 1, 'id': 'second-reply', 'revision': 2, 'at': '2041-02-09',
        'text': 'Use the device option and include notes.',
        'updates': [
            {'id': 'storage', 'status': 'answered', 'answer': 'Use the device option.', 'selected': 'device'},
            {'id': 'notes', 'status': 'answered', 'answer': 'Include notes.'},
        ],
    }
    answered = records.answer_document(tmp_path, f'{document_id}.md', next_answer)
    text = records.render_document(answered)
    history = records.history_path(tmp_path, f'{document_id}.md').read_text(encoding='utf-8')
    assert answered['presentation'] == 'decision-pending'
    assert '## 최신 답' in text and '**Device** — Keep local state.' in text
    assert '### Include notes?' in text
    assert '명세 갱신 필요: 새 답을 현재 명세에 반영해야 합니다.' in text
    assert f'[질문 정본 JSON](questions/{document_id}.json)' in text
    assert f'[이전 명세·답 원문 전체 이력](details/history/{document_id}.md)' in text
    assert f'### Include notes?\n\n[최신 답 원문](details/history/{document_id}.md)' in text
    assert '미답 0/2' in text and '별도 승인 요청: 승인 대기' in text
    assert 'Use the device option and include notes.' not in text
    assert 'Use the account option.' not in text
    assert 'Old intro sentinel.' not in text and 'Old approval reason sentinel.' not in text
    assert 'Choose whether to include notes.' not in text and 'Omit notes' not in text
    assert 'The list stores reading progress on the account.' not in text
    assert 'Old detail sentinel.' not in text and '[상세]' not in text
    assert answered['items'][0]['selected'] == 'device' and answered['items'][1]['status'] == 'answered'
    assert answered['answers'] == [first_answer, next_answer] and answered['approval'] == approval
    assert next_answer['text'] in history and '선택 id: `device`' in history
    assert 'Include notes.' in history
    assert not check(tmp_path / f'{document_id}.md', tmp_path / f'questions/{document_id}.json')['issues']
    assert 'The list stores reading progress on the account.' in records.read_document(
        tmp_path, f'{document_id}.md'
    )['blocks'][0]['sentences'][0]

    before_pending_replay = {
        str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()
    }
    pending_replay = records.answer_document(tmp_path, f'{document_id}.md', next_answer)
    after_pending_replay = {
        str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()
    }
    assert pending_replay['presentation'] == 'decision-pending'
    assert before_pending_replay == after_pending_replay

    reenabled = records.patch_document(answered, {
        'set': {'revision': 3, 'presentation': 'decision'},
        'upsert_blocks': [{
            'id': 'result', 'type': 'paragraph', 'title': 'Current result',
            'sentences': ['The list stores reading progress on the device and keeps notes.'],
        }],
    }, editorial=True)
    records.publish_document(tmp_path, reenabled, 2, editorial=True)
    archived = json.loads((tmp_path / f'questions/history/{document_id}/2.json').read_text(encoding='utf-8'))
    assert archived['blocks'][0]['sentences'] == ['The list stores reading progress on the account.']
    before_decision_replay = {
        str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()
    }
    replay = records.answer_document(tmp_path, f'{document_id}.md', next_answer)
    after_decision_replay = {
        str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()
    }
    assert replay['presentation'] == 'decision' and replay['answers'] == reenabled['answers']
    assert '## 현재 선택' not in records.render_document(replay)
    assert before_decision_replay == after_decision_replay


def test_decision_pending_hides_legacy_blocks_and_details_but_links_free_answer():
    record = draft()
    record['schema'] = 1
    record['body'] = ''
    record['blocks'] = [{'id': 'old-block', 'type': 'paragraph', 'text': 'Old block sentinel.'}]
    record['detail_blocks'] = [{'id': 'old-detail', 'type': 'paragraph', 'text': 'Old detail sentinel.'}]
    record['presentation'] = 'decision-pending'
    record['items'][0].update(status='answered', answer='A free response.')
    record['answers'] = [{
        'schema': 1, 'id': 'free-reply', 'revision': 1, 'at': '2041-02-10',
        'text': 'A free response.', 'updates': [
            {'id': 'storage', 'status': 'answered', 'answer': 'A free response.'},
        ],
    }]

    text = records.render_document(record)
    assert '명세 갱신 필요: 새 답을 현재 명세에 반영해야 합니다.' in text
    assert '### Save reading state?' in text and '[최신 답 원문](details/history/confirm-A2.md)' in text
    assert 'Old block sentinel.' not in text
    assert 'Old detail sentinel.' not in text and 'A free response.' not in text


def publish_answered_question(folder, identifier, *, table_contract=False):
    record = draft()
    record['id'] = identifier
    if table_contract:
        record['blocks'] = [
            {
                'id': 'contract-table', 'type': 'table', 'title': 'Current contract',
                'columns': ['필드', '타입'], 'rows': [['Identifier', 'Text']], 'row_ids': ['identifier'],
            },
            {
                'id': 'contract-fields', 'type': 'fields', 'title': 'Current fields',
                'fields': [{'name': 'Identifier', 'type': 'Text'}],
            },
            {
                'id': 'contract-literal', 'type': 'literal', 'title': 'Current token',
                'format': 'identifier', 'value': 'reading-list',
            },
        ]
    records.publish_document(folder, record)
    answer = {
        'schema': 1, 'id': 'resolved', 'revision': 1, 'at': '2041-02-11',
        'text': 'Use the account option.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the account option.', 'selected': 'account'}],
        'approval': {'status': 'complete', 'reason': 'Approved.'},
    }
    return records.answer_document(folder, f'{identifier}.md', answer)


@pytest.mark.parametrize('successors', [[], ['bad/id'], ['ask-old'], ['ask-next', 'ask-next']])
def test_superseded_requires_nonempty_safe_unique_nonself_successors(successors):
    record = draft()
    record['id'] = 'ask-old'
    record['intro'] = 'This question has been replaced.'
    record['items'][0].update(status='answered', answer='Use the account option.')
    record['approval'] = {'status': 'complete', 'reason': 'Approved.'}
    record['presentation'] = 'superseded'
    record['successors'] = successors
    with pytest.raises(records.QuestionRecordError):
        records.validate_document(record)


@pytest.mark.parametrize('pending_kind', ['item', 'approval'])
def test_superseded_rejects_unanswered_or_approval_pending_records(pending_kind):
    record = draft()
    record['id'] = 'ask-old'
    record['intro'] = 'This question has been replaced.'
    record['presentation'] = 'superseded'
    record['successors'] = ['ask-next']
    record['items'][0].update(status='answered', answer='Use the account option.')
    record['approval'] = {'status': 'complete', 'reason': 'Approved.'}
    if pending_kind == 'item':
        record['items'][0].update(status='pending', answer='')
    else:
        record['approval'] = {'status': 'pending', 'reason': 'Awaiting approval.'}
    with pytest.raises(records.QuestionRecordError, match='superseded로 표시할 수 없음'):
        records.validate_document(record)


def test_successors_are_only_valid_for_superseded_presentation():
    record = draft()
    record['successors'] = ['ask-next']
    with pytest.raises(records.QuestionRecordError, match='successors는 presentation superseded'):
        records.validate_document(record)


def test_superseded_publication_links_current_target_and_preserves_hidden_contract_in_archive(tmp_path):
    records.publish_document(tmp_path, draft() | {'id': 'ask-next'})
    current = publish_answered_question(tmp_path, 'ask-old', table_contract=True)
    superseded = records.patch_document(current, {
        'set': {
            'revision': 2,
            'intro': 'This question was replaced by the current version.',
            'presentation': 'superseded',
            'successors': ['ask-next'],
        },
    }, editorial=True)
    records.publish_document(tmp_path, superseded, 1, editorial=True)

    text = (tmp_path / 'ask-old.md').read_text(encoding='utf-8')
    saved = records.read_document(tmp_path, 'ask-old.md')
    archive = records.history_path(tmp_path, 'ask-old.md')
    result = check(tmp_path / 'ask-old.md', records.source_path(tmp_path, 'ask-old.md'), archive)
    assert 'This question was replaced by the current version.' in text
    assert '[ask-next](ask-next.md)' in text
    assert '[질문 정본 JSON](questions/ask-old.json)' in text and str(archive.name) in text
    assert 'Save reading state?' not in text and 'Account' not in text and 'Current contract' not in text
    assert {block['id'] for block in saved['blocks']} == {
        'contract-table', 'contract-fields', 'contract-literal',
    }
    assert not result['issues']


def test_pending_table_only_answer_has_hidden_contract_verified_by_human_check(tmp_path):
    record = draft()
    record['id'] = 'ask-table-only'
    record['blocks'] = [{
        'id': 'contract-table', 'type': 'table', 'title': 'Current contract',
        'columns': ['필드', '타입'], 'rows': [['Identifier', 'Text']], 'row_ids': ['identifier'],
    }]
    records.publish_document(tmp_path, record)
    first = records.answer_document(tmp_path, 'ask-table-only.md', {
        'schema': 1, 'id': 'first', 'revision': 1, 'at': '2041-02-13',
        'text': 'Use the account option.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the account option.', 'selected': 'account'}],
        'approval': {'status': 'complete', 'reason': 'Approved.'},
    })
    decision = records.patch_document(first, {'set': {'revision': 2, 'presentation': 'decision'}}, editorial=True)
    records.publish_document(tmp_path, decision, 1, editorial=True)
    records.answer_document(tmp_path, 'ask-table-only.md', {
        'schema': 1, 'id': 'second', 'revision': 2, 'at': '2041-02-14',
        'text': 'Use the device option.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the device option.', 'selected': 'device'}],
    })

    result = check(
        tmp_path / 'ask-table-only.md',
        records.source_path(tmp_path, 'ask-table-only.md'),
        records.history_path(tmp_path, 'ask-table-only.md'),
    )
    assert not result['issues']
    assert '명세 갱신 필요:' in (tmp_path / 'ask-table-only.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('contract_kind', ['table', 'fields', 'literal'])
def test_hidden_superseded_candidate_must_preserve_source_contract_even_with_archive(tmp_path, contract_kind):
    source_record = publish_answered_question(tmp_path, 'ask-old', table_contract=True)
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(source_record, ensure_ascii=False), encoding='utf-8')
    candidate = records.patch_document(source_record, {
        'set': {
            'revision': 2, 'intro': 'This question was replaced.',
            'presentation': 'superseded', 'successors': ['ask-next'],
        },
    }, editorial=True)
    target = tmp_path / 'candidate.json'
    target.write_text(json.dumps(candidate, ensure_ascii=False), encoding='utf-8')
    archive = records.history_path(tmp_path, 'ask-old.md')
    assert not check(target, source, archive)['issues']
    archive_bytes = archive.read_bytes()
    archive.unlink()
    assert any('전체 이력' in issue for issue in check(target, source, archive)['issues'])
    archive.write_bytes(archive_bytes)

    changed = copy.deepcopy(candidate)
    if contract_kind == 'table':
        changed['blocks'][0]['rows'][0][0] = 'ChangedIdentifier'
    elif contract_kind == 'fields':
        changed['blocks'][1]['fields'][0]['type'] = 'Number'
    else:
        changed['blocks'][2]['value'] = 'changed-token'
    target.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
    issues = check(target, source, archive)['issues']
    assert any('table/fields/literal 정본이 source 원문과 다릅니다' in issue for issue in issues)


def test_superseded_preflight_rejects_missing_successors_and_cycles(tmp_path):
    old = publish_answered_question(tmp_path, 'ask-old')
    missing = records.patch_document(old, {
        'set': {'revision': 2, 'intro': 'Replaced.', 'presentation': 'superseded', 'successors': ['ask-missing']},
    }, editorial=True)
    with pytest.raises(records.QuestionRecordError, match='successor 질문 문서를 확인할 수 없음'):
        records.preflight_document(tmp_path, missing, 1, editorial=True)

    following = publish_answered_question(tmp_path, 'ask-next')
    next_superseded = records.patch_document(following, {
        'set': {'revision': 2, 'intro': 'Replaced.', 'presentation': 'superseded', 'successors': ['ask-old']},
    }, editorial=True)
    records.publish_document(tmp_path, next_superseded, 1, editorial=True)
    cyclic = records.patch_document(old, {
        'set': {'revision': 2, 'intro': 'Replaced.', 'presentation': 'superseded', 'successors': ['ask-next']},
    }, editorial=True)
    with pytest.raises(records.QuestionRecordError, match='successor 순환'):
        records.preflight_document(tmp_path, cyclic, 1, editorial=True)


def test_new_answer_exits_superseded_and_removes_successor_metadata(tmp_path):
    records.publish_document(tmp_path, draft() | {'id': 'ask-next'})
    current = publish_answered_question(tmp_path, 'ask-old')
    superseded = records.patch_document(current, {
        'set': {'revision': 2, 'intro': 'Replaced.', 'presentation': 'superseded', 'successors': ['ask-next']},
    }, editorial=True)
    records.publish_document(tmp_path, superseded, 1, editorial=True)
    answer = {
        'schema': 1, 'id': 'updated', 'revision': 2, 'at': '2041-02-12',
        'text': 'Use the device option.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the device option.', 'selected': 'device'}],
    }
    updated = records.answer_document(tmp_path, 'ask-old.md', answer)
    assert updated['presentation'] == 'decision-pending'
    assert 'successors' not in updated
    text = (tmp_path / 'ask-old.md').read_text(encoding='utf-8')
    assert '명세 갱신 필요:' in text and '**Device**' in text and 'ask-next.md' not in text


def test_schema1_body_only_superseded_answer_uses_pending_and_preserves_raw_history(tmp_path):
    records.publish_document(tmp_path, draft() | {'id': 'ask-next'})
    record = draft()
    record.update(id='ask-legacy', schema=1, intro='Old intro sentinel.', body='Old body sentinel.')
    record.pop('blocks')
    record['items'][0]['body'] = 'Old pending question body sentinel.'
    records.publish_document(tmp_path, record)
    answered = records.answer_document(tmp_path, 'ask-legacy.md', {
        'schema': 1, 'id': 'first', 'revision': 1, 'at': '2041-02-15',
        'text': 'Use the account option.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the account option.', 'selected': 'account'}],
        'approval': {'status': 'complete', 'reason': 'Approved.'},
    })
    source = tmp_path / 'original.json'
    source.write_text(json.dumps(answered, ensure_ascii=False), encoding='utf-8')
    superseded = records.patch_document(answered, {
        'set': {
            'revision': 2, 'intro': 'This question was replaced.',
            'presentation': 'superseded', 'successors': ['ask-next'],
        },
    }, editorial=True)
    candidate = tmp_path / 'candidate.json'
    candidate.write_text(json.dumps(superseded, ensure_ascii=False), encoding='utf-8')
    archive = records.history_path(tmp_path, 'ask-legacy.md')
    assert not check(candidate, source, archive)['issues']

    changed = copy.deepcopy(superseded)
    changed['body'] = 'Changed old body.'
    candidate.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
    assert any('source body 원문' in issue for issue in check(candidate, source, archive)['issues'])

    records.publish_document(tmp_path, superseded, 1, editorial=True)
    latest = {
        'schema': 1, 'id': 'second', 'revision': 2, 'at': '2041-02-16',
        'text': 'Use the device option for this list.',
        'updates': [{'id': 'storage', 'status': 'answered', 'answer': 'Use the device option for this list.'}],
    }
    pending = records.answer_document(tmp_path, 'ask-legacy.md', latest)
    text = (tmp_path / 'ask-legacy.md').read_text(encoding='utf-8')
    history = records.history_path(tmp_path, 'ask-legacy.md').read_text(encoding='utf-8')
    assert pending['presentation'] == 'decision-pending' and 'successors' not in pending
    assert 'Old intro sentinel.' not in text and 'Old body sentinel.' not in text
    assert 'Old pending question body sentinel.' not in text and 'Account' not in text
    assert '### Save reading state?' in text and '[최신 답 원문](details/history/ask-legacy.md)' in text
    assert latest['text'] not in text and latest['text'] in history and 'Old body sentinel.' in history
    assert not check(tmp_path / 'ask-legacy.md', records.source_path(tmp_path, 'ask-legacy.md'))['issues']

    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    replay = records.answer_document(tmp_path, 'ask-legacy.md', latest)
    after = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert replay['presentation'] == 'decision-pending' and before == after


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
    record['blocks'] = []
    with pytest.raises(records.QuestionRecordError, match='전체 질문·확인 blocks'):
        records.validate_document(record)


@pytest.mark.parametrize(('identifier', 'presentation'), [('unsafe-id', 'decision'), ('ask-example', 'compact')])
def test_decision_view_rejects_unsupported_id_or_value(identifier, presentation):
    record = draft()
    record['id'] = identifier
    record['presentation'] = presentation
    with pytest.raises(records.QuestionRecordError):
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
