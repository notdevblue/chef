import json
import os
from collections import Counter
from pathlib import Path

import pytest

import usage_report as report

START = report.timestamp('2026-10-23T00:00:00+09:00')
END = report.timestamp('2026-10-24T00:00:00+09:00')


def row(request, at='2026-10-23T00:10:00+09:00', output=7):
    return {'type': 'assistant', 'timestamp': at, 'requestId': request, 'effort': 'high',
            'message': {'id': request, 'model': 'claude-sonnet-5-5', 'usage': {
                'input_tokens': 3, 'cache_creation_input_tokens': 11,
                'cache_read_input_tokens': 100, 'output_tokens': output,
                'iterations': [{'type': 'advisor_message', 'input_tokens': 150, 'output_tokens': 9}]}}}


def transcript(home, project, rows, child=False):
    root = home / 'projects' / report.slots.project_dir_name(str(project))
    path = root / ('s/subagents/agent-a.jsonl' if child else 's.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return path


def test_streaming_duplicates_cache_and_advisor_are_counted_separately(tmp_path):
    project = tmp_path / 'game'
    first, last = row('one', output=1), row('one', output=7)
    transcript(tmp_path, project, [first, last])
    diagnostics = Counter()
    records, missing = report.collect_usage([('main', project)], tmp_path, START, END, diagnostics)
    result = report.aggregate(records)
    assert result['responses'] == 1 and result['advisor_calls'] == 1
    assert result['usage'] == dict(zip(report.TOKEN_FIELDS, (3, 11, 100, 7)))
    assert result['advisor_usage'] == dict(zip(report.TOKEN_FIELDS, (150, 0, 0, 9)))
    assert not missing and not diagnostics


def test_timezone_boundaries_child_role_and_corrupt_tail(tmp_path):
    project = tmp_path / 'game'
    path = transcript(tmp_path, project, [row('before', '2026-10-22T14:59:59Z'),
        row('start', '2026-10-22T15:00:00Z'), row('end', '2026-10-23T15:00:00Z')], True)
    path.with_suffix('.meta.json').write_text('{"agentType":"code-reviewer"}', encoding='utf-8')
    with path.open('a', encoding='utf-8') as stream:
        stream.write('{unfinished')
    diagnostics = Counter()
    records, _ = report.collect_usage([('slot1', project)], tmp_path, START, END, diagnostics)
    assert len(records) == 1 and records[0]['role'] == 'code-reviewer'
    assert records[0]['effort'] == 'high'
    assert diagnostics['malformed_records'] == 1


def test_missing_metadata_unknown_iteration_and_no_request_id(tmp_path):
    project = tmp_path / 'game'
    record = row('one')
    del record['requestId']
    record['message']['usage']['iterations'].append({'type': 'future_tool'})
    transcript(tmp_path, project, [record], True)
    diagnostics = Counter()
    records, _ = report.collect_usage([('main', project)], tmp_path, START, END, diagnostics)
    assert records[0]['role'] == 'unknown-subagent'
    assert diagnostics['fallback_message_keys'] == 1
    assert diagnostics['unknown_iterations'] == 1


def test_quality_retains_unknown_counts_and_marks_legacy_time(tmp_path):
    root = tmp_path / 'state'
    directory = root / 'm1' / 'verification'
    directory.mkdir(parents=True)
    legacy = directory / 'M1-review-old.json'
    legacy.write_text(json.dumps({'schema': 1, 'unit': 'M1', 'status': 'findings'}), encoding='utf-8')
    os.utime(legacy, (START.timestamp(), START.timestamp()))
    (directory / 'M1-review-new.json').write_text(json.dumps({
        'schema': 1, 'unit': 'M1', 'status': 'clean', 'recordedAt': '2026-10-23T05:00:00+09:00',
        'findingsCount': 0, 'fixedCount': 2}), encoding='utf-8')
    (root / 'm1' / 'ledger.tsv').write_text('\t'.join(report.LEDGER_HEADER) +
        '\n2026-10-23T05:00:00+09:00\tM1\tretry\tpytest-pass\t"literal quote\tsrc.py\n', encoding='utf-8')
    quality = report.quality_report([('main', tmp_path)], {'main': root}, START, END, Counter())[0]
    assert quality['reviewRounds'] == 2
    assert len(quality['recordedRetries']) == 1
    old = next(r for r in quality['reviews'] if r['timeSource'] == 'file_mtime')
    assert old['findingsCount'] == -1 and old['fixedCount'] == -1


def test_quality_keeps_closed_low_status_and_ignores_review_start_records(tmp_path):
    root = tmp_path / 'state'
    directory = root / 'm1' / 'verification'
    directory.mkdir(parents=True)
    (directory / 'M1-review-low.json').write_text(json.dumps({
        'schema': 1, 'unit': 'M1', 'status': 'closed-low', 'recordedAt': '2026-10-23T05:00:00+09:00',
        'findingsCount': 0, 'fixedCount': 1, 'findings': [], 'resolvedIds': ['F1']}), encoding='utf-8')
    (directory / 'M1-reviewstart-abc.json').write_text(json.dumps({
        'schema': 1, 'unit': 'M1', 'recordedAt': '2026-10-23T05:00:00+09:00', 'snapshot': {}}), encoding='utf-8')
    (directory / 'M1.review-start.latest.json').write_text(json.dumps({'reviewStart': 'x'}), encoding='utf-8')
    diagnostics = Counter()
    quality = report.quality_report([('main', tmp_path)], {'main': root}, START, END, diagnostics)[0]
    assert [item['status'] for item in quality['reviews']] == ['closed-low']
    assert quality['verificationAttempts'] == [] and not diagnostics['invalid_review_records']


def test_cli_reports_missing_projects_and_does_not_export_content(tmp_path, capsys):
    project = tmp_path / 'game'
    record = row('one')
    record['message']['content'] = [{'type': 'text', 'text': 'private-content-sentinel'}]
    transcript(tmp_path, project, [record])
    output = tmp_path / 'report.json'
    assert report.main(['--project', f'main={project}', '--project', f'slot={tmp_path / "missing"}',
        '--mission-root', f'main={tmp_path / "no-state"}', '--mission-root', f'slot={tmp_path / "no-state"}',
        '--claude-home', str(tmp_path), '--start', START.isoformat(), '--end', END.isoformat(),
        '--output', str(output)]) == 0
    text = output.read_text(encoding='utf-8')
    assert 'private-content-sentinel' not in text
    assert len(json.loads(text)['missingTranscriptDirectories']) == 1
    assert 'responses=1' in capsys.readouterr().out


def test_timestamp_requires_timezone():
    with pytest.raises(ValueError):
        report.timestamp('2026-10-23T00:00:00')


def test_missing_adapter_is_reported_without_losing_usage(tmp_path, monkeypatch):
    def unavailable(*args):
        raise report.slots.UsageError('missing')
    monkeypatch.setattr(report.slots, 'locate', unavailable)
    quality = report.quality_report([('main', tmp_path)], {}, START, END, Counter())
    assert quality == [{'project': 'main', 'available': False, 'reason': 'UsageError'}]


def test_relative_project_path_resolves_to_the_same_transcript_directory(tmp_path, monkeypatch):
    project = tmp_path / 'game'
    project.mkdir()
    transcript(tmp_path, project, [row('one')])
    monkeypatch.chdir(project)
    records, missing = report.collect_usage([('main', Path('.'))], tmp_path, START, END, Counter())
    assert len(records) == 1 and not missing


def test_invalid_adapter_state_path_keeps_usage_available(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(report.slots, 'locate', lambda *args: SimpleNamespace(project=str(tmp_path), adapter_path='unused'))
    monkeypatch.setattr(report.slots, 'read_adapter', lambda *args: {'schema': 1})
    quality = report.quality_report([('main', tmp_path)], {}, START, END, Counter())
    assert quality == [{'project': 'main', 'available': False, 'reason': 'GuardOff'}]


def activity_file(tmp_path, activities):
    path = tmp_path / 'activities.json'
    path.write_text(json.dumps({'schema': 1, 'activities': activities}), encoding='utf-8')
    return report.load_activity_map(path)


def test_explicit_activity_differs_from_role_and_preserves_all_totals(tmp_path):
    project = tmp_path / 'game'
    record = row('one')
    record['sessionId'] = 'session-one'
    record['agentId'] = 'a'
    path = transcript(tmp_path, project, [record, row('two')], True)
    path.with_suffix('.meta.json').write_text('{"agentType":"reviewer"}', encoding='utf-8')
    records, _ = report.collect_usage([('main', project)], tmp_path, START, END, Counter())
    before = report.aggregate(records)
    activities = activity_file(tmp_path, [{'sessionId': 'session-one', 'agentId': 'a',
        'taskKind': 'document', 'mission': 'sample-mission', 'unit': 'U1', 'phase': 'draft', 'attempt': 2}])
    report.assign_activities(records, activities)
    known = next(item for item in records if item['taskKind'] == 'document')
    assert known['role'] == 'reviewer'
    assert known['activitySource'] == 'activity-map'
    assert known['attempt'] == '2'
    assert sum(item['taskKind'] == 'unknown' for item in records) == 1
    assert before == report.aggregate(records)
    groups = report.grouped(records, report.ACTIVITY_FIELDS)
    assert sum(item['responses'] for item in groups) == before['responses']
    for field in report.TOKEN_FIELDS:
        assert sum(item['usage'][field] for item in groups) == before['usage'][field]
        assert sum(item['advisor_usage'][field] for item in groups) == before['advisor_usage'][field]


def test_concurrent_sessions_and_agents_never_deduplicate_one_another(tmp_path):
    project = tmp_path / 'game'
    records = [dict(row('same'), sessionId=session) for session in ('first', 'second')]
    transcript(tmp_path, project, records)
    child = dict(row('same'), sessionId='first', agentId='a')
    transcript(tmp_path, project, [child], True)
    usage, _ = report.collect_usage([('main', project)], tmp_path, START, END, Counter())
    assert len(usage) == 3
    report.assign_activities(usage, activity_file(tmp_path, [
        {'sessionId': 'first', 'taskKind': 'coordination'},
        {'sessionId': 'first', 'agentId': 'a', 'taskKind': 'verification'}]))
    assert {(item['sessionId'], item['agentId'], item['taskKind']) for item in usage} == {
        ('first', '', 'coordination'), ('second', '', 'unknown'), ('first', 'a', 'verification')}


@pytest.mark.parametrize(('maps', 'metadata', 'status'), [
    ([], '', 'unknown'),
    ([{'taskKind': 'review'}, {'taskKind': 'review'}], '', 'ambiguous'),
    ([{'taskKind': 'review'}, {'taskKind': 'document'}], '', 'conflict'),
    ([{'taskKind': 'review'}], 'implementation', 'conflict'),
    ([], 'research', 'assigned'),
])
def test_activity_missing_ambiguous_conflicting_and_actual_metadata(tmp_path, maps, metadata, status):
    project = tmp_path / 'game'
    record = dict(row('one'), sessionId='session-one')
    if metadata:
        record['taskKind'] = metadata
    record['message']['content'] = [{'text': 'implementation review document'}]
    transcript(tmp_path, project, [record])
    records, _ = report.collect_usage([('main', project)], tmp_path, START, END, Counter())
    activities = activity_file(tmp_path, [dict(item, sessionId='session-one') for item in maps])
    report.assign_activities(records, activities)
    assert records[0]['activityStatus'] == status
    if status != 'assigned':
        assert records[0]['taskKind'] == 'unknown'
    else:
        assert records[0]['taskKind'] == 'research'
        assert records[0]['mission'] == 'unknown'


def test_activity_intervals_are_half_open_and_streaming_boundary_is_unknown(tmp_path):
    project = tmp_path / 'game'
    transcript(tmp_path, project, [dict(row('one'), sessionId='session-one'),
        dict(row('two', at='2026-10-23T00:20:00+09:00'), sessionId='session-one')])
    records, _ = report.collect_usage([('main', project)], tmp_path, START, END, Counter())
    activities = activity_file(tmp_path, [{'sessionId': 'session-one', 'taskKind': 'review',
        'startedAt': '2026-10-23T00:10:00+09:00', 'endedAt': '2026-10-23T00:20:00+09:00'}])
    report.assign_activities(records, activities)
    assert [item['taskKind'] for item in records] == ['review', 'unknown']
    repeated = dict(row('one', at='2026-10-23T00:20:00+09:00'), sessionId='session-one')
    transcript(tmp_path, project, [dict(row('one'), sessionId='session-one'), repeated])
    records, _ = report.collect_usage([('main', project)], tmp_path, START, END, Counter())
    report.assign_activities(records, activities)
    assert len(records) == 1 and records[0]['activityStatus'] == 'ambiguous'


@pytest.mark.parametrize('activity', [
    {'sessionId': 's'}, {'sessionId': 's', 'taskKind': 'invented'},
    {'sessionId': 's', 'taskKind': 'review', 'attempt': True},
    {'sessionId': 's', 'taskKind': 'review', 'startedAt': '2026-10-23T00:00:00'},
    {'sessionId': 's', 'taskKind': 'review', 'startedAt': END.isoformat(), 'endedAt': START.isoformat()},
])
def test_invalid_activity_maps_do_not_invent_classifications(tmp_path, activity):
    with pytest.raises(ValueError):
        activity_file(tmp_path, [activity])


def test_quality_typed_attempts_reuse_failures_legacy_and_period_boundaries(tmp_path):
    root = tmp_path / 'state'
    directory = root / 'mission-one' / 'verification'
    directory.mkdir(parents=True)
    def write(name, value):
        path = directory / name
        path.write_text(json.dumps(value), encoding='utf-8')
        os.utime(path, (START.timestamp(), START.timestamp()))
    base = {'schema': 1, 'unit': 'U1', 'recordedAt': START.isoformat(), 'status': 'fail',
        'startedAt': START.isoformat(), 'finishedAt': '2026-10-23T00:00:02+09:00', 'seconds': 2,
        'error': 'raw-failure-sentinel', 'plan': {'commands': [{'argv': ['prompt-sentinel']}]},
        'executionCounts': {'executed': 1, 'reused': 1},
        'steps': [{'index': 0, 'status': 'reused', 'seconds': 0},
                  {'index': 1, 'status': 'failed', 'seconds': 2, 'exitCode': 1}],
        'failure': {'category': 'command_exit', 'step': 1, 'signature': 'a' * 64, 'repeatCount': 2}}
    write('U1-first.json', base)
    write('U1-old.json', {'schema': 1, 'unit': 'U1', 'status': 'pass', 'seconds': 1})
    write('U1-end.json', dict(base, recordedAt=END.isoformat()))
    write('U1-invalid.json', dict(base, recordedAt=''))
    write('U1.latest.json', {'receipt': 'U1-first.json'})
    (root / 'mission-one' / 'ledger.tsv').write_text('\t'.join(report.LEDGER_HEADER) +
        '\n' + START.isoformat() + '\tU1\tretry\tpytest-pass\tsame failure\tsource.py\n', encoding='utf-8')
    diagnostics = Counter()
    quality = report.quality_report([('main', tmp_path)], {'main': root}, START, END, diagnostics)[0]
    assert diagnostics['invalid_verification_records'] == 1
    assert quality['verificationSummary'] == {'attempts': 2, 'failed': 1, 'executedSteps': 1,
        'reusedSteps': 1, 'unknownExecutionCounts': 1,
        'retryCounting': 'typed receipt attempts and legacy ledger retries are separate; do not add'}
    assert len(quality['recordedRetries']) == 1
    typed = next(item for item in quality['verificationAttempts'] if item['status'] == 'fail')
    assert typed['failure']['repeatCount'] == 2
    assert typed['failure']['category'] == 'command_exit'
    assert [step['status'] for step in typed['steps']] == ['reused', 'failed']
    assert typed['startedAt'] and typed['finishedAt'] and typed['seconds'] == 2
    legacy = next(item for item in quality['verificationAttempts'] if item['status'] == 'pass')
    assert legacy['timeSource'] == 'file_mtime' and legacy['failure']['category'] == 'unknown'
    assert 'raw-failure-sentinel' not in json.dumps(quality)
    assert 'prompt-sentinel' not in json.dumps(quality)


def test_structured_review_counts_only_recorded_identities_and_kinds():
    structured = {'findings': [
        {'id': 'd1', 'kind': 'defect', 'origin': 'new'},
        {'id': 'd2', 'kind': 'defect', 'origin': 'unresolved', 'reason': 'raw-reason-sentinel'},
        {'id': 'd3', 'kind': 'defect', 'origin': 'regression', 'relatedId': 'prior'},
        {'id': 'r1', 'kind': 'rule_violation', 'origin': 'new'}],
        'suggestions': [{'text': 'raw-suggestion-sentinel'}], 'resolvedIds': ['prior'],
        'findingsCount': 400, 'fixedCount': 300}
    result = report.review_findings(structured)
    assert result == {'identitySource': 'structured', 'byKind': {
        'defect': {'new': 1, 'unresolved': 1, 'regression': 1},
        'rule_violation': {'new': 1, 'unresolved': 0, 'regression': 0}}, 'suggestions': 1, 'resolved': 1}
    assert 'sentinel' not in json.dumps(result)
    legacy = report.review_findings({'findingsCount': 4, 'fixedCount': 3})
    assert legacy['identitySource'] == 'unknown'
    assert legacy['byKind']['defect']['new'] == -1
    structured['findings'].append(structured['findings'][0])
    assert report.review_findings(structured)['identitySource'] == 'unknown'


def test_cli_activity_map_serializes_groups_without_exporting_transcript_content(tmp_path):
    project = tmp_path / 'game'
    record = dict(row('one'), sessionId='session-one')
    record['message']['content'] = [{'text': 'transcript-body-sentinel'}]
    record['toolInput'] = {'text': 'tool-input-sentinel'}
    transcript(tmp_path, project, [record])
    path = tmp_path / 'activities.json'
    path.write_text(json.dumps({'schema': 1, 'activities': [
        {'sessionId': 'session-one', 'taskKind': 'document', 'phase': 'draft'}]}), encoding='utf-8')
    output = tmp_path / 'usage.json'
    report.main(['--project', f'main={project}', '--mission-root', f'main={tmp_path / "absent"}',
        '--claude-home', str(tmp_path), '--start', START.isoformat(), '--end', END.isoformat(),
        '--activity-map', str(path), '--output', str(output)])
    serialized = output.read_text(encoding='utf-8')
    result = json.loads(serialized)
    assert 'sentinel' not in serialized
    assert result['byActivity'][0]['taskKind'] == 'document'
    assert result['byActivity'][0]['phase'] == 'draft'
    assert result['byActivity'][0]['mission'] == 'unknown'
    assert result['byActivity'][0]['usage'] == result['total']['usage']
    assert result['activityCoverage'][0]['activitySource'] == 'activity-map'
