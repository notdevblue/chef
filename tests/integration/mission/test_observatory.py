import json
from pathlib import Path

import pytest
from chef.mission import observatory as obs

from tests.paths import PLUGIN_ROOT


def test_malformed_identity_and_nested_model_cannot_export_raw_content(tmp_path):
    item = source(tmp_path, 'claude', [{'type': 'assistant', 'sessionId': {'raw': 'PRIVATE BODY'},
        'agentId': [], 'requestId': [], 'timestamp': '2026-01-01T00:00:00Z',
        'message': {'id': {}, 'model': {'raw': 'PRIVATE BODY'},
                    'usage': {'iterations': 8, 'input_tokens': 2}}}])
    report = obs.collect_report([item])
    exported = obs.serialize_report(report)
    assert 'PRIVATE BODY' not in exported
    assert report['records'][0]['model'] == 'unknown'
    assert report['diagnostics']['invalid_advisor_iterations'] == 1
    item = source(tmp_path, 'codex', [{'type': 'turn_context', 'payload': {'model': {'raw': 'PRIVATE BODY'}}},
                                    token_row({'input_tokens': 2})])
    assert 'PRIVATE BODY' not in obs.serialize_report(obs.collect_report([item]))


def test_invalid_utf8_source_is_diagnostic_not_raw_export(tmp_path):
    path = tmp_path / 'broken.jsonl'
    path.write_bytes(b'\xff\xfeprivate')
    report = obs.collect_report([{'runtime': 'claude', 'path': path}])
    assert report['diagnostics']['unreadable_sources'] == 1
    assert report['records'] == []


def test_execution_failure_rate_uses_explicit_outcomes_only(tmp_path):
    item = source(tmp_path, 'manual', [{'schema': 1, 'kind': 'runtime_metadata', 'outcome': outcome}
                                     for outcome in ('success', 'failure', 'interrupted', 'unknown', 'invalid')])
    report = obs.collect_report([item])
    assert len(report['records']) == 4
    assert report['total'][0]['failureRate'] == .5
    assert report['total'][0]['outcomes'] == {'success': 1, 'failure': 1, 'interrupted': 1, 'unknown': 1}
    assert report['diagnostics']['invalid_metadata'] == 1


def source(tmp_path, runtime, rows):
    path = tmp_path / (runtime + '.jsonl')
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return {'runtime': runtime, 'path': path}


def token_row(total, at='2026-01-01T00:00:00Z'):
    return {'type': 'event_msg', 'timestamp': at, 'payload': {'type': 'token_count', 'info': {'total_token_usage': total}}}


def test_codex_cumulative_duplicates_partial_reset_and_time_filter(tmp_path):
    item = source(tmp_path, 'codex', [
        {'type': 'session_meta', 'payload': {'id': 's'}},
        {'type': 'turn_context', 'payload': {'model': 'observed', 'effort': 'high'}},
        token_row({'input_tokens': 10, 'output_tokens': 2}),
        token_row({'input_tokens': 10, 'output_tokens': 2}),
        token_row({'input_tokens': 15}, '2026-01-01T00:01:00Z'),
        token_row({'input_tokens': 18, 'output_tokens': 4}, '2026-01-01T00:02:00Z'),
        token_row({'input_tokens': 3, 'output_tokens': 1}, '2026-01-01T00:03:00Z')])
    report = obs.collect_report([item], start='2026-01-01T00:01:00Z')
    assert [row['tokens']['input_tokens'] for row in report['records']] == [5, 3, 3]
    assert report['total'][0]['tokens']['output_tokens'] == 3
    assert report['total'][0]['coverage']['output_tokens'] == {'known': 2, 'unknown': 1}
    assert report['diagnostics']['duplicate_cumulative_records'] == 1
    assert report['diagnostics']['cumulative_resets'] == 1
    assert report['records'][0]['model'] == 'observed'
    assert report['records'][0]['requestedModel'] == 'unknown'


def test_claude_stream_maximum_unknown_and_no_content_export(tmp_path):
    def row(output):
        return {'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z', 'requestId': 'r',
                'sessionId': 's', 'message': {'id': 'm', 'model': 'observed', 'content': 'PRIVATE BODY',
                    'usage': {'input_tokens': 10, 'output_tokens': output}}}
    item = source(tmp_path, 'claude', [row(1), row(3)])
    report = obs.collect_report([item, item])
    assert len(report['records']) == 1
    assert report['total'][0]['tokens']['output_tokens'] == 3
    assert report['total'][0]['tokens']['cache_creation_input_tokens'] is None
    output = tmp_path / 'report.json'
    obs.export_report(report, output)
    text = output.read_text()
    assert 'PRIVATE BODY' not in text and str(item['path']) not in text
    assert report['diagnostics']['duplicate_sources'] == 1


def test_grok_unknown_does_not_claim_zero_usage(tmp_path):
    item = source(tmp_path, 'grok', [{'timestamp': '2026-01-01T00:00:00Z', 'params': {
        'sessionId': 'g', 'update': {'sessionUpdate': 'turn_completed'}}}])
    report = obs.collect_report([item])
    assert report['total'][0]['tokens']['input_tokens'] is None
    assert report['cost'] is None and report['credits'] is None
    assert report['records'][0]['model'] == 'unknown'


def test_manual_strict_metadata_and_malformed_tail(tmp_path):
    item = source(tmp_path, 'manual', [
        {'schema': 1, 'kind': 'runtime_metadata', 'runtime': 'codex', 'role': 'review',
         'mission': 'm', 'tokens': {'input_tokens': 5}, 'durationSeconds': 2, 'retries': 1},
        {'schema': 1, 'kind': 'runtime_metadata', 'tokens': {'output_tokens': True}},
        {'schema': 1, 'kind': 'runtime_metadata', 'raw': 'PRIVATE'},
        {'schema': 1, 'kind': 'runtime_metadata', 'durationSeconds': float('inf')}])
    with item['path'].open('a') as stream:
        stream.write('{partial')
    report = obs.collect_report([item])
    assert len(report['records']) == 1
    assert report['byMission'][0]['mission'] == 'm'
    assert report['byRole'][0]['retries'] == 1
    assert report['diagnostics']['invalid_metadata'] == 3
    assert report['diagnostics']['malformed_records'] == 1
    csv = tmp_path / 'report.csv'
    obs.export_report(report, csv, 'csv')
    assert 'input_tokens' in csv.read_text() and 'PRIVATE' not in csv.read_text()


def test_schema_versions_and_imported_raw_fields_rejected(tmp_path):
    migrated = obs.normalize_report({'schema': 1, 'total': {'usage': {'input_tokens': 1}}, 'source': 'PRIVATE'})
    assert migrated['schema'] == 2 and migrated['total'][0]['tokens']['input_tokens'] == 1
    assert migrated['diagnostics']['legacy_aggregate_only'] == 1
    with pytest.raises(ValueError):
        obs.normalize_report({'schema': 999})
    row = obs.empty_record('codex')
    row['source'] = 'PRIVATE'
    with pytest.raises(ValueError):
        obs.export_report({'schema': 2, 'records': [row]}, tmp_path / 'bad.json')


def test_explicit_activity_mapping_preserves_unknown_without_guess(tmp_path):
    item = source(tmp_path, 'manual', [{'schema': 1, 'kind': 'runtime_metadata', 'sessionId': 's',
        'timestamp': '2026-01-01T00:00:00Z', 'role': 'code-reviewer'}])
    report = obs.collect_report([item])
    assert report['records'][0]['taskKind'] == 'unknown'
    mapping = tmp_path / 'map.json'
    mapping.write_text(json.dumps({'schema': 1, 'activities': [{'sessionId': 's', 'taskKind': 'review', 'mission': 'm'}]}))
    mapped = obs.collect_report([item], activities=obs.legacy.load_activity_map(mapping))
    assert mapped['records'][0]['mission'] == 'm'
    assert mapped['records'][0]['activityStatus'] == 'assigned'


def test_quality_export_sanitized_and_summary_preserved(tmp_path):
    report = obs.collect_report([], quality=[{'project': 'synthetic', 'verificationAttempts': [
        {'status': 'pass', 'source': '/private/log', 'seconds': 2}, {'status': 'fail'}],
        'reviews': [{'findingsCount': 2}], 'recordedRetries': [{'unit': 'u'}], 'raw': 'PRIVATE'}])
    path = tmp_path / 'quality.json'
    obs.export_report(report, path)
    exported = json.loads(path.read_text())
    assert exported['qualitySummary']['passRate'] == .5
    assert exported['qualitySummary']['roundFindings'] == 2
    assert 'PRIVATE' not in path.read_text() and '/private/log' not in path.read_text()
    obs.export_report(report, path, 'csv')
    assert 'quality' in path.read_text() and 'PRIVATE' not in path.read_text()


def test_strict_normalization_recomputes_aggregates(tmp_path):
    row = obs.empty_record('manual')
    row['tokens']['input_tokens'] = 3
    report = obs.normalize_report({'schema': 2, 'records': [row], 'total': {'raw': 'PRIVATE'}, 'raw': 'PRIVATE'})
    assert report['total'][0]['tokens']['input_tokens'] == 3
    assert 'raw' not in report
    del row['phase']
    with pytest.raises(ValueError):
        obs.normalize_report({'schema': 2, 'records': [row]})


@pytest.mark.parametrize('attempt', [{'raw': 'PRIVATE BODY'}, [], True, -1, '', 'x' * 257])
def test_attempt_cannot_export_structures_or_unbounded_metadata(tmp_path, attempt):
    row = obs.empty_record('manual')
    row['attempt'] = attempt
    with pytest.raises(ValueError):
        obs.export_report({'schema': 2, 'records': [row]}, tmp_path / 'report.json')


@pytest.mark.parametrize('quality', [
    [1], [{'reviews': [None]}], [{'verificationAttempts': [1]}],
    [{'verificationAttempts': [{'executionCounts': {'executed': {'raw': 'PRIVATE BODY'}}}]}],
    [{'reviews': [{'findingsCount': 'PRIVATE BODY'}]}],
    [{'verificationAttempts': [{'seconds': float('nan')}]}],
    [{'verificationSummary': {'failed': float('inf')}}],
])
def test_quality_corruption_fails_with_value_error(quality):
    with pytest.raises(ValueError):
        obs.normalize_report({'schema': 2, 'records': [], 'quality': quality})


@pytest.mark.parametrize('generated', [None, {}, 123, 'PRIVATE BODY', '2026-01-01T00:00:00'])
def test_generated_at_is_validated(generated):
    with pytest.raises(ValueError):
        obs.normalize_report({'schema': 2, 'records': [], 'generatedAt': generated})


def test_legacy_totals_preserve_values_without_claiming_observed_coverage(tmp_path):
    report = obs.normalize_report({'schema': 1, 'total': {'usage': {'input_tokens': 0, 'output_tokens': 7}}})
    total = report['total'][0]
    assert total['tokens']['input_tokens'] == 0 and total['tokens']['output_tokens'] == 7
    assert total['records'] == 0 and total['aggregateRecords'] == 1
    assert total['coverageBasis'] == 'aggregate_unknown'
    assert total['coverage']['input_tokens'] == {'known': None, 'unknown': None}
    assert report['diagnostics']['legacy_missingness_unknown'] == 1
    target = tmp_path / 'migration.json'
    obs.export_report(report, target)
    assert json.loads(target.read_text())['total'][0]['records'] == 0


@pytest.mark.parametrize('value', ['=SUM(1,2)', '+1+2', '-1+2', '@SUM(A1)', '\t=1+1', '  =1+1'])
def test_csv_formula_metadata_is_escaped(tmp_path, value):
    import csv
    row = obs.empty_record('manual')
    row['project'] = value
    target = tmp_path / 'report.csv'
    obs.export_report({'schema': 2, 'records': [row]}, target, 'csv')
    with target.open() as stream:
        exported = list(csv.DictReader(stream))
    assert exported[0]['project'] == "'" + value


def test_pure_serialization_reuses_validated_csv_and_json():
    row = obs.empty_record('manual')
    row['project'] = '=1+1'
    report = {'schema': 2, 'records': [row]}
    assert json.loads(obs.serialize_report(report))['records'][0]['project'] == '=1+1'
    assert "'=1+1" in obs.serialize_report(report, 'csv')
