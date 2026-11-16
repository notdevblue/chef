"""Run Metrics (실행 통계): collect explicitly selected runtime metadata, read-only."""
import argparse
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import usage_report as legacy
import grok_transcript
from mission_state import write_text

SCHEMA = 2
RUNTIMES = ('claude', 'codex', 'grok', 'manual')
TOKEN_FIELDS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')
DIMENSIONS = ('runtime', 'project', 'mission', 'phase', 'role', 'model', 'effort', 'taskKind', 'unit')
CAPABILITIES = {
    'claude': {'tokens': 'assistant.message.usage', 'model': 'message.model', 'effort': 'recorded only'},
    'codex': {'tokens': 'event_msg.token_count.info cumulative delta', 'model': 'turn_context', 'effort': 'turn_context'},
    'grok': {'tokens': 'unsupported; use explicit manual metadata', 'model': 'explicit metadata only', 'effort': 'explicit metadata only'},
    'manual': {'tokens': 'schema 1 explicit metadata', 'model': 'observed value only', 'effort': 'observed value only'},
}


def empty_record(runtime, project='unknown'):
    return dict(runtime=runtime, project=project, mission='unknown', phase='unknown', unit='unknown',
                role='unknown', model='unknown', effort='unknown', requestedModel='unknown',
                requestedEffort='unknown', taskKind='unknown', sessionId='', agentId='', timestamp='',
                tokens=dict.fromkeys(TOKEN_FIELDS), durationSeconds=None, retries=None, cost=None, credits=None,
                activityStatus='unknown', activitySource='none', evidence='recorded',
                outcome='unknown', failureCategory='unknown')


def nonnegative(value):
    return type(value) in (int, float) and value >= 0 and __import__('math').isfinite(value)


def counts(value, aliases=None):
    value = value if isinstance(value, dict) else {}
    aliases = aliases or {}
    return {field: value.get(aliases.get(field, field)) if type(value.get(aliases.get(field, field))) is int
            and value[aliases.get(field, field)] >= 0 else None for field in TOKEN_FIELDS}


def metadata_text(value, default='unknown'):
    return value if isinstance(value, str) and value else default


def lines(path, diagnostics):
    try:
        with Path(path).open(encoding='utf-8-sig') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('object required')
                    yield row
                except (ValueError, TypeError):
                    diagnostics['malformed_records'] += 1
    except (OSError, UnicodeError):
        diagnostics['unreadable_sources'] += 1


def stamp(value, diagnostics):
    try:
        return legacy.timestamp(value).isoformat()
    except (ValueError, TypeError):
        diagnostics['missing_or_invalid_timestamps'] += 1
        return ''


def selected(record, start, end):
    if not (start or end):
        return True
    if not record['timestamp']:
        return False
    at = legacy.timestamp(record['timestamp'])
    return (start is None or start <= at) and (end is None or at < end)


def claude_records(source, diagnostics):
    records = {}
    advisors = {}
    for row in lines(source['path'], diagnostics):
        if row.get('type') != 'assistant':
            continue
        message = row.get('message')
        if not isinstance(message, dict):
            diagnostics['malformed_records'] += 1
            continue
        usage = message.get('usage')
        if not isinstance(usage, dict):
            diagnostics['missing_usage'] += 1
            usage = {}
        session = metadata_text(row.get('sessionId'), '')
        key = (session, metadata_text(row.get('agentId'), ''),
               metadata_text(row.get('requestId'), ''), metadata_text(message.get('id'), ''))
        if not key[2] or not key[3]:
            key = (*key, metadata_text(row.get('uuid'), '') or len(records))
            diagnostics['fallback_message_keys'] += 1
        record = records.setdefault(key, empty_record('claude', source.get('project', 'unknown')))
        record.update(sessionId=session, agentId=metadata_text(row.get('agentId'), ''),
                      model=metadata_text(message.get('model')),
                      effort=metadata_text(row.get('perTurnEffort') or row.get('effort')),
                      role=source.get('role', 'unknown'), timestamp=stamp(row.get('timestamp'), diagnostics))
        # Preserve missingness; legacy merge_counts provides streaming maximum semantics.
        known = {key: value for key, value in counts(usage).items() if value is not None}
        merged = {key: value for key, value in record['tokens'].items() if value is not None}
        legacy.merge_counts(merged, known)
        record['tokens'].update(merged)
        iterations = usage.get('iterations') or []
        if not isinstance(iterations, list):
            diagnostics['invalid_advisor_iterations'] += 1
            iterations = []
        for index, iteration in enumerate(iterations):
            if isinstance(iteration, dict) and iteration.get('type') == 'advisor_message':
                known = {k: v for k, v in counts(iteration).items() if v is not None}
                legacy.merge_counts(advisors.setdefault(key, {}).setdefault(index, {}), known)
    for key, record in records.items():
        if key in advisors:
            record['advisorTokens'] = {field: sum(item[field] for item in advisors[key].values() if field in item)
                                      if any(field in item for item in advisors[key].values()) else None
                                      for field in TOKEN_FIELDS}
        yield record


def codex_records(source, diagnostics):
    session, agent, model, effort = '', '', 'unknown', 'unknown'
    previous = {}
    for row in lines(source['path'], diagnostics):
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        if row.get('type') == 'session_meta':
            session = metadata_text(payload.get('id'), '')
            agent = metadata_text(payload.get('agent_id'), '')
        elif row.get('type') == 'turn_context':
            model = metadata_text(payload.get('model'))
            effort = metadata_text(payload.get('reasoning_effort') or payload.get('effort'))
        elif row.get('type') == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info')
            if not isinstance(info, dict) or not isinstance(info.get('total_token_usage'), dict):
                diagnostics['missing_usage'] += 1
                continue
            current = counts(info['total_token_usage'], {'cache_read_input_tokens': 'cached_input_tokens',
                                                        'reasoning_output_tokens': 'reasoning_output_tokens'})
            known = {key: value for key, value in current.items() if value is not None}
            if not known:
                diagnostics['missing_usage'] += 1
                continue
            if all(previous.get(key) == value for key, value in known.items()):
                diagnostics['duplicate_cumulative_records'] += 1
                continue
            reset = any(key in previous and value < previous[key] for key, value in known.items())
            if reset:
                diagnostics['cumulative_resets'] += 1
            record = empty_record('codex', source.get('project', 'unknown'))
            record.update(sessionId=session, agentId=agent, model=model, effort=effort,
                          role=source.get('role', 'unknown'), timestamp=stamp(row.get('timestamp'), diagnostics))
            # On reset the observed new counter is a fresh segment, not a negative delta.
            record['tokens'] = {key: (value if reset or key not in previous else value - previous[key])
                                if value is not None else None for key, value in current.items()}
            previous = known if reset else {**previous, **known}
            yield record


def grok_records(source, diagnostics):
    for row in lines(source['path'], diagnostics):
        params = row.get('params')
        if not isinstance(params, dict) or not isinstance(params.get('update'), dict):
            continue
        update = params['update']
        if update.get('sessionUpdate') != 'turn_completed':
            continue
        record = empty_record('grok', source.get('project', 'unknown'))
        record.update(sessionId=metadata_text(params.get('sessionId'), ''), role=source.get('role', 'unknown'),
                      timestamp=grok_transcript.timestamp_of(row, params))
        if 'usage' in update:
            diagnostics['unsupported_grok_usage_shape'] += 1
        if all(value is None for value in record['tokens'].values()):
            diagnostics['missing_usage'] += 1
        yield record


def manual_records(source, diagnostics):
    allowed = set(empty_record('manual')) - {'runtime', 'cost', 'credits', 'evidence', 'activityStatus', 'activitySource'}
    for row in lines(source['path'], diagnostics):
        if (type(row.get('schema')) is not int or row['schema'] != 1 or row.get('kind') != 'runtime_metadata'
                or set(row) - {'schema', 'kind', 'runtime', *allowed}):
            diagnostics['invalid_metadata'] += 1
            continue
        runtime = row.get('runtime', 'manual')
        if runtime not in RUNTIMES:
            diagnostics['invalid_metadata'] += 1
            continue
        record = empty_record(runtime, source.get('project', 'unknown'))
        invalid = False
        for key, value in row.items():
            if key not in allowed:
                continue
            if key == 'tokens':
                invalid |= not isinstance(value, dict) or bool(set(value) - set(TOKEN_FIELDS))
                if isinstance(value, dict):
                    invalid |= any(v is not None and (type(v) is not int or v < 0) for v in value.values())
                    record[key] = counts(value)
            elif key in ('durationSeconds', 'retries'):
                invalid |= value is not None and (not nonnegative(value) or key == 'retries' and type(value) is not int)
                record[key] = value
            else:
                invalid |= not isinstance(value, str)
                record[key] = value
        if invalid:
            diagnostics['invalid_metadata'] += 1
            continue
        if record['taskKind'] not in (*legacy.TASK_KINDS, 'unknown'):
            diagnostics['invalid_metadata'] += 1
            continue
        if record['outcome'] not in ('unknown', 'success', 'failure', 'interrupted'):
            diagnostics['invalid_metadata'] += 1
            continue
        if record['timestamp']:
            normalized = stamp(record['timestamp'], diagnostics)
            if not normalized:
                diagnostics['invalid_metadata'] += 1
                continue
            record['timestamp'] = normalized
        yield record


def aggregate_records(records, by=()):
    if any(field not in DIMENSIONS for field in by):
        raise ValueError('unsupported grouping')
    groups = defaultdict(list)
    for record in records:
        groups[tuple(record.get(field, 'unknown') for field in by)].append(record)
    result = []
    for identity, rows in sorted(groups.items()):
        item = dict(zip(by, identity))
        aggregate_evidence = any(row.get('evidence') == 'legacy_aggregate' for row in rows)
        item.update(records=sum(row.get('evidence') != 'legacy_aggregate' for row in rows),
                    aggregateRecords=sum(row.get('evidence') == 'legacy_aggregate' for row in rows),
                    coverageBasis='aggregate_unknown' if aggregate_evidence else 'observed_records', tokens={}, coverage={})
        for key in TOKEN_FIELDS:
            known = [row['tokens'][key] for row in rows if row['tokens'][key] is not None]
            item['tokens'][key] = sum(known) if known else None
            item['coverage'][key] = ({'known': None, 'unknown': None} if aggregate_evidence else
                                     {'known': len(known), 'unknown': len(rows) - len(known)})
        for key in ('durationSeconds', 'retries'):
            values = [row[key] for row in rows if row.get(key) is not None]
            item[key] = sum(values) if values else None
        item['advisorTokens'] = {}
        item['advisorCoverage'] = {}
        for key in TOKEN_FIELDS:
            known = [row.get('advisorTokens', {}).get(key) for row in rows
                     if row.get('advisorTokens', {}).get(key) is not None]
            item['advisorTokens'][key] = sum(known) if known else None
            item['advisorCoverage'][key] = ({'known': None, 'unknown': None} if aggregate_evidence else
                                            {'known': len(known), 'unknown': len(rows) - len(known)})
        item.update(cost=None, credits=None)
        outcomes = Counter(row.get('outcome', 'unknown') for row in rows if row.get('evidence') != 'legacy_aggregate')
        known_outcomes = outcomes['success'] + outcomes['failure']
        item['outcomes'] = {name: outcomes[name] for name in ('success', 'failure', 'interrupted', 'unknown')}
        item['failureRate'] = outcomes['failure'] / known_outcomes if known_outcomes else None
        result.append(item)
    return result


def collect_quality(projects, mission_roots, start, end):
    """Reuse receipt/review aggregation without exporting paths or log content."""
    diagnostics = Counter()
    rows = legacy.quality_report(projects, mission_roots, legacy.timestamp(start) if isinstance(start, str) else start,
                                 legacy.timestamp(end) if isinstance(end, str) else end, diagnostics)
    return safe_quality(rows), dict(diagnostics)


def safe_quality(value):
    """Validate typed receipt metadata, dropping unsupported content/path fields."""
    strings = {'project', 'mission', 'unit', 'status', 'at', 'timeSource', 'startedAt', 'finishedAt',
               'category', 'id', 'kind', 'identitySource'}
    integers = {'reviewRounds', 'executed', 'reused', 'repeatCount', 'step', 'index', 'findingsCount',
                'fixedCount', 'round', 'suggestions', 'resolved', 'attempts', 'failed', 'executedSteps',
                'reusedSteps', 'unknownExecutionCounts', 'new', 'unresolved', 'regression'}
    list_fields = {'reviews', 'recordedRetries', 'verificationAttempts', 'steps'}
    object_fields = {'verificationSummary', 'executionCounts', 'failure', 'findings', 'byKind',
                     'defect', 'rule_violation'}
    def object_value(item):
        if not isinstance(item, dict):
            raise ValueError('quality entries must be objects')
        result = {}
        for key, child in item.items():
            if key in strings:
                if not isinstance(child, str):
                    raise ValueError('invalid quality string')
                result[key] = child
            elif key in integers:
                if type(child) is not int or child < -1:
                    raise ValueError('invalid quality counter')
                result[key] = child
            elif key == 'available':
                if type(child) is not bool:
                    raise ValueError('invalid quality availability')
                result[key] = child
            elif key == 'seconds':
                if type(child) not in (int, float) or not __import__('math').isfinite(child) or child < -1:
                    raise ValueError('invalid quality duration')
                result[key] = child
            elif key == 'exitCode':
                if type(child) is not int and child != 'unknown':
                    raise ValueError('invalid quality exit code')
                result[key] = child
            elif key in list_fields:
                if not isinstance(child, list):
                    raise ValueError('quality collection must be a list')
                result[key] = [object_value(entry) for entry in child]
            elif key in object_fields:
                result[key] = object_value(child)
        return result
    if not isinstance(value, list):
        raise ValueError('quality must be a list')
    return [object_value(item) for item in value]


def quality_summary(rows):
    attempts = [attempt for row in rows for attempt in row.get('verificationAttempts', [])]
    passed = sum(attempt.get('status') == 'pass' for attempt in attempts)
    failed = sum(attempt.get('status') == 'fail' for attempt in attempts)
    findings = [review.get('findingsCount') for row in rows for review in row.get('reviews', [])]
    known_findings = [value for value in findings if type(value) is int and value >= 0]
    return {'verificationAttempts': len(attempts), 'passed': passed, 'failed': failed,
            'passRate': passed / (passed + failed) if passed + failed else None,
            'failureRate': failed / (passed + failed) if passed + failed else None,
            'recordedRetries': sum(len(row.get('recordedRetries', [])) for row in rows),
            'roundFindings': sum(known_findings) if known_findings else None,
            'unknownFindingRounds': len(findings) - len(known_findings)}


def collect_report(sources, start=None, end=None, activities=None, quality=None):
    if isinstance(start, str):
        start = legacy.timestamp(start)
    if isinstance(end, str):
        end = legacy.timestamp(end)
    if start and end and start >= end:
        raise ValueError('start must precede end')
    diagnostics = Counter()
    records = []
    readers = dict(claude=claude_records, codex=codex_records, grok=grok_records, manual=manual_records)
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or source.get('runtime') not in readers or not source.get('path'):
            raise ValueError('explicit runtime and path required')
        if 'home' in source:
            raise ValueError('home scanning unsupported; select explicit transcript files')
        if any(key in source and (not isinstance(source[key], str) or not source[key]) for key in ('project', 'role')):
            raise ValueError('source project/role must be metadata strings')
        identity = (source['runtime'], str(Path(source['path']).resolve()))
        if identity in seen:
            diagnostics['duplicate_sources'] += 1
            continue
        seen.add(identity)
        records.extend(record for record in readers[source['runtime']](source, diagnostics) if selected(record, start, end))
    if activities:
        for record in records:
            record['timestamps'] = {legacy.timestamp(record['timestamp'])} if record['timestamp'] else set()
            record['taskKinds'] = {record['taskKind']} if record['taskKind'] in legacy.TASK_KINDS else set()
        legacy.assign_activities(records, activities)
        for record in records:
            record.pop('timestamps', None)
            record.pop('taskKinds', None)
    result = {'schema': SCHEMA, 'generatedAt': datetime.now(timezone.utc).isoformat(),
              'records': records, 'capabilities': CAPABILITIES, 'diagnostics': dict(diagnostics),
              'quality': safe_quality(quality) if quality is not None else [], 'cost': None, 'credits': None,
              'limitations': ['Known sums exclude missing observations.', 'Tokens do not measure quota or credits.',
                              'Cumulative reset begins a new observed segment.', 'Activity labels require explicit evidence.']}
    result['qualitySummary'] = quality_summary(result['quality'])
    result['total'] = aggregate_records(records)
    result['byMission'] = aggregate_records(records, ('mission',))
    result['byPhase'] = aggregate_records(records, ('phase',))
    result['byRole'] = aggregate_records(records, ('runtime', 'role'))
    result['byModel'] = aggregate_records(records, ('runtime', 'model', 'effort'))
    return result


def normalize_report(report):
    if not isinstance(report, dict) or type(report.get('schema')) is not int:
        raise ValueError('report schema required')
    if report['schema'] == 1 and isinstance(report.get('total'), dict) and isinstance(report['total'].get('usage'), dict):
        row = empty_record('claude')
        row['tokens'] = counts(report['total']['usage'])
        if isinstance(report['total'].get('advisor_usage'), dict):
            row['advisorTokens'] = counts(report['total']['advisor_usage'])
        row['evidence'] = 'legacy_aggregate'
        report = {'schema': 2, 'records': [row], 'diagnostics': {'legacy_aggregate_only': 1, 'legacy_missingness_unknown': 1},
                  'quality': safe_quality(report.get('quality', [])), 'generatedAt': report.get('generatedAt', '')}
    if report['schema'] != SCHEMA or not isinstance(report.get('records'), list):
        raise ValueError('unsupported report schema')
    generated_at = report.get('generatedAt', '')
    if not isinstance(generated_at, str):
        raise ValueError('generatedAt must be an ISO timestamp string')
    if generated_at:
        legacy.timestamp(generated_at)
    records = []
    for row in report['records']:
        required = set(empty_record('manual'))
        if not isinstance(row, dict) or required - set(row) or set(row) - required - {'advisorTokens', 'attempt'}:
            raise ValueError('unsupported or missing record fields')
        if row['runtime'] not in RUNTIMES or not isinstance(row['tokens'], dict):
            raise ValueError('invalid normalized record')
        if row.get('outcome') not in ('unknown', 'success', 'failure', 'interrupted'):
            raise ValueError('invalid execution outcome')
        for name in ('tokens', 'advisorTokens'):
            if name in row and (not isinstance(row[name], dict) or set(row[name]) != set(TOKEN_FIELDS) or
                    any(value is not None and (type(value) is not int or value < 0) for value in row[name].values())):
                raise ValueError('invalid token observations')
        for key in required - {'tokens', 'durationSeconds', 'retries', 'cost', 'credits'}:
            if not isinstance(row[key], str):
                raise ValueError('invalid metadata string')
        if 'attempt' in row and not (type(row['attempt']) is int and row['attempt'] >= 0 or
                                      isinstance(row['attempt'], str) and 0 < len(row['attempt']) <= 256
                                      and row['attempt'].strip()):
            raise ValueError('attempt must be a nonnegative integer or bounded string')
        if row['timestamp']:
            legacy.timestamp(row['timestamp'])
        if any(row[key] is not None and (not nonnegative(row[key]) or key == 'retries' and type(row[key]) is not int)
               for key in ('durationSeconds', 'retries')) or row['cost'] is not None or row['credits'] is not None:
            raise ValueError('invalid numeric observation')
        records.append(dict(row))
    diagnostics = report.get('diagnostics', {})
    if not isinstance(diagnostics, dict) or any(not isinstance(key, str) or type(value) is not int or value < 0
                                                for key, value in diagnostics.items()):
        raise ValueError('invalid diagnostics')
    quality = safe_quality(report.get('quality', []))
    if not isinstance(quality, list):
        raise ValueError('quality must be a list')
    result = {'schema': SCHEMA, 'records': records, 'generatedAt': generated_at,
              'diagnostics': diagnostics, 'quality': quality, 'qualitySummary': quality_summary(quality),
              'capabilities': CAPABILITIES, 'cost': None, 'credits': None,
              'limitations': ['Unknown observations are null; known sums may be partial.',
                              'Legacy aggregates cannot reconstruct individual requests.']}
    result['total'] = aggregate_records(records)
    for key, by in (('byMission', ('mission',)), ('byPhase', ('phase',)),
                    ('byRole', ('runtime', 'role')), ('byModel', ('runtime', 'model', 'effort'))):
        result[key] = aggregate_records(records, by)
    return result


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def serialize_report(report, format='json'):
    """Return a validated metadata-only export without writing files."""
    report = normalize_report(report)
    # Only normalized fields are emitted; arbitrary imported objects never become raw content exports.
    safe = {key: report[key] for key in ('schema', 'generatedAt', 'records', 'capabilities', 'diagnostics',
                                        'total', 'byMission', 'byPhase', 'byRole', 'byModel', 'limitations', 'cost', 'credits', 'quality', 'qualitySummary') if key in report}
    if 'quality' in safe:
        safe['quality'] = safe_quality(safe['quality'])
    if format == 'json':
        text = json.dumps(safe, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    elif format == 'csv':
        stream = io.StringIO(newline='')
        fields = ['recordType', 'qualityMetadata', *DIMENSIONS, 'sessionId', 'agentId', 'timestamp', *TOKEN_FIELDS,
                  'durationSeconds', 'retries', 'outcome', 'failureCategory', 'cost', 'credits']
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for record in report['records']:
            writer.writerow({**{key: csv_cell(record['tokens'].get(key) if key in TOKEN_FIELDS else record.get(key)) for key in fields}, 'recordType': 'runtime'})
        for quality in report.get('quality', []):
            writer.writerow({'recordType': 'quality', 'qualityMetadata': json.dumps(quality, ensure_ascii=False)})
        text = stream.getvalue()
    else:
        raise ValueError('format must be json or csv')
    return text


def export_report(report, output, format='json'):
    write_text(str(output), serialize_report(report, format))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True, help='runtime=explicit JSONL path')
    parser.add_argument('--project', action='append', type=legacy.named_path, default=[])
    parser.add_argument('--mission-root', action='append', type=legacy.named_path, default=[])
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--activity-map', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--format', choices=('json', 'csv'), default='json')
    args = parser.parse_args(argv)
    sources = []
    for value in args.source:
        runtime, separator, path = value.partition('=')
        if not separator or runtime not in RUNTIMES or not path:
            parser.error('--source needs runtime=path')
        sources.append({'runtime': runtime, 'path': path})
    try:
        report = collect_report(sources, args.start, args.end, legacy.load_activity_map(args.activity_map))
        if args.project:
            if not args.start or not args.end:
                parser.error('--project quality collection requires --start and --end')
            report['quality'], diagnostics = collect_quality(args.project, dict(args.mission_root), args.start, args.end)
            report['diagnostics'].update(diagnostics)
        export_report(report, args.output, args.format)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"records={len(report['records'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
