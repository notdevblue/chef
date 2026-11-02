import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'slots' / 'scripts'))
import slots
import mission_guard
from mission_state import LEDGER_HEADER, StateError, read_table

TOKEN_FIELDS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')
TASK_KINDS = ('implementation', 'document', 'review', 'verification', 'coordination', 'research')
ACTIVITY_FIELDS = ('taskKind', 'mission', 'unit', 'phase', 'attempt')
FAILURE_CATEGORIES = ('plan_invalid', 'preflight', 'guard_rejected', 'launch_error', 'timeout',
                      'command_exit', 'result_invalid', 'input_changed', 'runtime_changed')


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('ISO 시각 문자열이 필요함')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('시각에 시간대가 필요함')
    return result


def named_path(value):
    name, separator, path = value.partition('=')
    if not separator or not name.strip() or not path.strip():
        raise argparse.ArgumentTypeError('이름=경로 형식이 필요함')
    return name, Path(path)


def token_counts(usage, diagnostics):
    result = {}
    for field in TOKEN_FIELDS:
        value = usage.get(field, 0)
        if field not in usage and field in ('input_tokens', 'output_tokens'):
            diagnostics['missing_token_fields'] += 1
        if type(value) is not int or value < 0:
            diagnostics['invalid_token_fields'] += 1
            value = 0
        result[field] = value
    return result


def merge_counts(target, source):
    for key, value in source.items():
        target[key] = max(target.get(key, 0), value)


def read_json(path, diagnostics):
    try:
        result = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(result, dict):
            return result
    except (OSError, ValueError):
        pass
    diagnostics['unreadable_metadata'] += 1
    return {}


def load_activity_map(path):
    if path is None:
        return []
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or value.get('schema') != 1 or not isinstance(value.get('activities'), list):
        raise ValueError('activity-map schema=1 및 activities 배열이 필요함')
    result = []
    for activity in value['activities']:
        if not isinstance(activity, dict):
            raise ValueError('activity-map 항목은 객체여야 함')
        if any(not isinstance(activity.get(field), str) or not activity[field].strip()
               for field in ('sessionId', 'taskKind')):
            raise ValueError('activity-map 식별자·작업 분류 문자열이 필요함')
        if activity['taskKind'] not in TASK_KINDS:
            raise ValueError('activity-map taskKind가 올바르지 않음')
        if any(field in activity and (not isinstance(activity[field], str) or not activity[field].strip())
               for field in ('mission', 'unit', 'phase')):
            raise ValueError('activity-map mission/unit/phase는 비어 있지 않은 문자열이어야 함')
        attempt = activity.get('attempt', 'unknown')
        if not (isinstance(attempt, str) and attempt.strip() or type(attempt) is int and attempt >= 0):
            raise ValueError('activity-map attempt 문자열 또는 0 이상 정수가 필요함')
        if not isinstance(activity.get('agentId', ''), str):
            raise ValueError('activity-map agentId는 문자열이어야 함')
        item = {field: str(activity.get(field, 'unknown')) for field in ACTIVITY_FIELDS}
        item.update(sessionId=activity['sessionId'], agentId=activity.get('agentId', ''))
        for field in ('startedAt', 'endedAt'):
            item[field] = timestamp(activity[field]) if field in activity else None
        if item['startedAt'] is not None and item['endedAt'] is not None and item['startedAt'] >= item['endedAt']:
            raise ValueError('activity-map startedAt는 endedAt보다 앞서야 함')
        result.append(item)
    return result


def assign_activities(records, activities):
    for record in records:
        record.update(dict.fromkeys(ACTIVITY_FIELDS, 'unknown'))
        record['activityStatus'] = 'unknown'
        record['activitySource'] = 'none'
        assignments = []
        ambiguous = False
        unmatched = False
        for at in record['timestamps']:
            matches = [item for item in activities
                       if (item['sessionId'], item['agentId']) == (record['sessionId'], record['agentId'])
                       and (item['startedAt'] is None or item['startedAt'] <= at)
                       and (item['endedAt'] is None or at < item['endedAt'])]
            ambiguous = ambiguous or len(matches) > 1
            unmatched = unmatched or not matches
            assignments.extend(tuple(item[field] for field in ACTIVITY_FIELDS) for item in matches)
        distinct = set(assignments)
        metadata = record['taskKinds']
        if assignments:
            record['activitySource'] = 'activity-map+metadata' if metadata else 'activity-map'
        elif metadata:
            record['activitySource'] = 'metadata'
        if len(distinct) > 1 or len(metadata) > 1 or distinct and metadata and next(iter(distinct))[0] not in metadata:
            record['activityStatus'] = 'conflict'
        elif ambiguous or distinct and unmatched:
            record['activityStatus'] = 'ambiguous'
        elif distinct:
            record.update(zip(ACTIVITY_FIELDS, next(iter(distinct))))
            record['activityStatus'] = 'assigned'
            record['activitySource'] = 'activity-map+metadata' if metadata else 'activity-map'
        elif metadata:
            record['taskKind'] = next(iter(metadata))
            record['activityStatus'] = 'assigned'
            record['activitySource'] = 'metadata'


def collect_usage(projects, home, start, end, diagnostics):
    records = {}
    missing = []
    for label, project in projects:
        directory = home / 'projects' / slots.project_dir_name(str(project.resolve()))
        if not directory.is_dir():
            missing.append(str(directory))
            continue
        for path in sorted(directory.rglob('*.jsonl')):
            child = path.parent.name == 'subagents'
            meta = path.with_suffix('.meta.json')
            metadata = read_json(meta, diagnostics) if meta.exists() else {}
            role = 'coordinator'
            if child:
                role = str(metadata.get('agentType') or 'unknown-subagent')
            try:
                stream = path.open(encoding='utf-8-sig')
            except OSError:
                diagnostics['unreadable_transcripts'] += 1
                continue
            with stream:
                for number, line in enumerate(stream, 1):
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('record')
                        if row.get('type') != 'assistant':
                            continue
                        at = timestamp(row['timestamp'])
                        if not start <= at < end:
                            continue
                        message = row['message']
                        usage = message['usage']
                        if not isinstance(usage, dict):
                            raise ValueError('usage')
                    except (ValueError, KeyError, TypeError):
                        diagnostics['malformed_records'] += 1
                        continue
                    request = row.get('requestId', '')
                    message_id = message.get('id', '')
                    session = str(row.get('sessionId') or metadata.get('sessionId') or (path.parent.parent.name if child else path.stem))
                    agent = str(row.get('agentId') or metadata.get('agentId') or path.stem.removeprefix('agent-')) if child else ''
                    identity = (label, session, agent)
                    key = (*identity, request, message_id) if request and message_id else (*identity, str(path), row.get('uuid', number))
                    if not request or not message_id:
                        diagnostics['fallback_message_keys'] += 1
                    effort = row.get('perTurnEffort') or row.get('effort') or 'unknown'
                    if not isinstance(effort, str):
                        effort = 'unknown'
                    record = records.setdefault(key, {
                        'project': label, 'role': role, 'model': str(message.get('model') or 'unknown'),
                        'effort': effort, 'source': str(path), 'usage': {}, 'advisor': {},
                        'sessionId': session, 'agentId': agent, 'timestamps': set(), 'taskKinds': set(),
                    })
                    record['timestamps'].add(at)
                    for source in (row, metadata):
                        if source.get('taskKind') in TASK_KINDS:
                            record['taskKinds'].add(source['taskKind'])
                    merge_counts(record['usage'], token_counts(usage, diagnostics))
                    for index, iteration in enumerate(usage.get('iterations') or []):
                        if not isinstance(iteration, dict):
                            diagnostics['unknown_iterations'] += 1
                            continue
                        if iteration.get('type') == 'advisor_message':
                            counts = token_counts(iteration, diagnostics)
                            merge_counts(record['advisor'].setdefault(index, {}), counts)
                        elif iteration.get('type') != 'message':
                            diagnostics['unknown_iterations'] += 1
    return list(records.values()), missing


def aggregate(records):
    result = {'responses': 0, 'advisor_calls': 0, 'usage': dict.fromkeys(TOKEN_FIELDS, 0),
              'advisor_usage': dict.fromkeys(TOKEN_FIELDS, 0)}
    for record in records:
        result['responses'] += 1
        result['advisor_calls'] += len(record['advisor'])
        for field in TOKEN_FIELDS:
            result['usage'][field] += record['usage'].get(field, 0)
            result['advisor_usage'][field] += sum(value.get(field, 0) for value in record['advisor'].values())
    return result


def grouped(records, fields):
    groups = defaultdict(list)
    for record in records:
        groups[tuple(record[field] for field in fields)].append(record)
    return [dict(zip(fields, key), **aggregate(values)) for key, values in sorted(groups.items())]


def recorded_time(value, path):
    if 'recordedAt' in value:
        return timestamp(value['recordedAt']), 'recordedAt'
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), 'file_mtime'


def count_value(value, default=-1):
    if type(value) is int and value >= 0:
        return value
    return default


def duration_value(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return -1


def review_findings(review):
    findings = review.get('findings')
    if not isinstance(findings, list) or not all(
        isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']
        and item.get('kind') in ('defect', 'rule_violation') and item.get('origin') in ('new', 'unresolved', 'regression')
        for item in findings
    ) or len({item['id'] for item in findings}) != len(findings):
        return {'identitySource': 'unknown', 'byKind': {kind: dict.fromkeys(('new', 'unresolved', 'regression'), -1)
                                                     for kind in ('defect', 'rule_violation')},
                'suggestions': -1, 'resolved': -1}
    resolved = review.get('resolvedIds')
    suggestions = review.get('suggestions')
    return {'identitySource': 'structured',
            'byKind': {kind: {origin: sum(item['kind'] == kind and item['origin'] == origin for item in findings)
                             for origin in ('new', 'unresolved', 'regression')}
                       for kind in ('defect', 'rule_violation')},
            'suggestions': len(suggestions) if isinstance(suggestions, list) else -1,
            'resolved': len(set(resolved)) if isinstance(resolved, list) and all(isinstance(item, str) for item in resolved) else -1}


def verification_attempt(receipt, path, at, source):
    result = {
        'mission': path.parent.parent.name, 'unit': receipt['unit'], 'status': receipt['status'],
        'at': at.isoformat(), 'timeSource': source, 'source': str(path),
        'seconds': duration_value(receipt.get('seconds')), 'startedAt': '', 'finishedAt': '',
        'executionCounts': {'executed': -1, 'reused': -1}, 'steps': [],
        'failure': {'category': 'unknown', 'signature': '', 'repeatCount': -1, 'step': -1},
    }
    for field in ('startedAt', 'finishedAt'):
        try:
            result[field] = timestamp(receipt[field]).isoformat()
        except (ValueError, KeyError, TypeError):
            pass
    counts = receipt.get('executionCounts')
    if isinstance(counts, dict):
        result['executionCounts'] = {field: count_value(counts.get(field)) for field in ('executed', 'reused')}
    steps = receipt.get('steps')
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get('status') not in ('passed', 'failed', 'reused', 'not_run'):
                continue
            result['steps'].append({
                'index': count_value(step.get('index')), 'id': step.get('id', '') if isinstance(step.get('id', ''), str) else '',
                'status': step['status'],
                'seconds': duration_value(step.get('seconds')),
                'exitCode': step.get('exitCode') if type(step.get('exitCode')) is int else 'unknown',
            })
    failure = receipt.get('failure')
    if isinstance(failure, dict):
        category = failure.get('category', '')
        signature = failure.get('signature', '')
        result['failure'] = {
            'category': category if category in FAILURE_CATEGORIES else ('none' if category == '' else 'unknown'),
            'signature': signature if isinstance(signature, str) and re.fullmatch(r'[a-f0-9]{64}', signature) else '',
            'repeatCount': count_value(failure.get('repeatCount')), 'step': count_value(failure.get('step')),
        }
    return result


def quality_report(projects, mission_roots, start, end, diagnostics):
    results = []
    for label, project in projects:
        root = mission_roots.get(label)
        if root is None:
            try:
                located = slots.locate(str(project), 'claude')
                adapter = slots.read_adapter(located.adapter_path, 'claude')
                root = Path(mission_guard.state_dir_of(located.project, adapter))
            except (ValueError, OSError, RuntimeError, slots.UsageError, mission_guard.GuardOff) as error:
                results.append({'project': label, 'available': False, 'reason': type(error).__name__})
                continue
        if not root.is_dir():
            results.append({'project': label, 'available': False, 'reason': 'missing mission root'})
            continue
        reviews = []
        retries = []
        attempts = []
        for path in sorted(root.glob('*/verification/*-review-*.json')):
            review = read_json(path, diagnostics)
            if review.get('schema') != 1 or 'unit' not in review:
                diagnostics['invalid_review_records'] += 1
                continue
            try:
                at, source = recorded_time(review, path)
            except (ValueError, TypeError, OSError):
                diagnostics['invalid_review_records'] += 1
                continue
            if start <= at < end:
                reviews.append({
                    'mission': path.parent.parent.name, 'unit': review['unit'], 'status': review.get('status', 'unknown'),
                    'kind': review.get('kind', 'unknown'), 'findingsCount': count_value(review.get('findingsCount')),
                    'fixedCount': count_value(review.get('fixedCount')), 'at': at.isoformat(), 'timeSource': source,
                    'round': count_value(review.get('round')),
                    'findings': review_findings(review),
                })
        for path in sorted(root.glob('*/verification/*.json')):
            if '-review-' in path.name or path.name.endswith('.latest.json'):
                continue
            receipt = read_json(path, diagnostics)
            if receipt.get('schema') != 1 or receipt.get('status') not in ('pass', 'fail') or 'unit' not in receipt:
                continue
            try:
                at, source = recorded_time(receipt, path)
            except (ValueError, TypeError, OSError):
                diagnostics['invalid_verification_records'] += 1
                continue
            if start <= at < end:
                attempts.append(verification_attempt(receipt, path, at, source))
        for path in sorted(root.glob('*/ledger.tsv')):
            try:
                for row in read_table(str(path), LEDGER_HEADER):
                    if row.get('status') != 'retry':
                        continue
                    try:
                        at = timestamp(row['at'])
                    except (ValueError, TypeError, KeyError):
                        diagnostics['invalid_retry_records'] += 1
                        continue
                    if start <= at < end:
                        retries.append({'mission': path.parent.name, 'unit': row.get('unit', ''), 'at': at.isoformat()})
            except (OSError, StateError):
                diagnostics['unreadable_ledgers'] += 1
        results.append({'project': label, 'available': True, 'reviewRounds': len(reviews),
                        'reviews': reviews, 'recordedRetries': retries, 'verificationAttempts': attempts,
                        'verificationSummary': {
                            'attempts': len(attempts), 'failed': sum(item['status'] == 'fail' for item in attempts),
                            'executedSteps': sum(item['executionCounts']['executed'] for item in attempts if item['executionCounts']['executed'] >= 0),
                            'reusedSteps': sum(item['executionCounts']['reused'] for item in attempts if item['executionCounts']['reused'] >= 0),
                            'unknownExecutionCounts': sum(-1 in item['executionCounts'].values() for item in attempts),
                            'retryCounting': 'typed receipt attempts and legacy ledger retries are separate; do not add',
                        }})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Claude 사용량·Advisor·리뷰 기록을 모델 호출 없이 집계합니다.')
    parser.add_argument('--project', action='append', type=named_path, required=True)
    parser.add_argument('--mission-root', action='append', type=named_path, default=[])
    parser.add_argument('--claude-home', type=Path, default=Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude'))
    parser.add_argument('--start', type=timestamp, required=True)
    parser.add_argument('--end', type=timestamp, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--activity-map', type=Path)
    args = parser.parse_args(argv)
    if args.start >= args.end:
        parser.error('start는 end보다 앞서야 함')
    labels = [label for label, _ in args.project]
    paths = [str(path.resolve()).casefold() for _, path in args.project]
    if len(set(labels)) != len(labels) or len(set(paths)) != len(paths):
        parser.error('프로젝트 이름·경로 중복')
    if any(label not in labels for label, _ in args.mission_root):
        parser.error('mission-root 이름에 해당하는 project가 없음')
    try:
        activities = load_activity_map(args.activity_map)
    except (OSError, ValueError, TypeError) as error:
        parser.error(f'activity-map 읽기 실패: {type(error).__name__}')
    diagnostics = Counter()
    records, missing = collect_usage(args.project, args.claude_home, args.start, args.end, diagnostics)
    assign_activities(records, activities)
    quality = quality_report(args.project, dict(args.mission_root), args.start, args.end, diagnostics)
    result = {
        'schema': 1, 'generatedAt': datetime.now(timezone.utc).isoformat(),
        'start': args.start.isoformat(), 'end': args.end.isoformat(),
        'total': aggregate(records), 'byProject': grouped(records, ('project',)),
        'byRole': grouped(records, ('project', 'role')), 'byModel': grouped(records, ('model', 'effort')),
        'byActivity': grouped(records, ('project', *ACTIVITY_FIELDS)),
        'activityCoverage': grouped(records, ('activityStatus', 'activitySource')),
        'quality': quality, 'missingTranscriptDirectories': missing, 'diagnostics': dict(diagnostics),
        'limitations': ['토큰 합계는 요금·구독 한도 소비율이 아님', 'byModel은 호출한 실행 모델 기준이며 Advisor 모델별 집계가 아님',
                        '지적 수 -1은 미기록; 라운드별 지적은 고유 결함 수가 아님',
                        'retry는 명시적으로 기록된 재작업만 포함', 'Advisor 토큰은 일반 메시지 usage와 별도 집계',
                        '작업 분류는 명시적 activity-map 또는 실제 taskKind 메타데이터만 사용하며 역할에서 추정하지 않음',
                        'unknown·ambiguous·conflict 작업은 분류하지 않으며 메타데이터만 있으면 미션·단계는 unknown',
                        '구조화된 리뷰 분류는 라운드별 기록이며 라운드 합계는 고유 결함 수가 아님',
                        '오래된 검증·리뷰의 file_mtime은 기록 시각 대신 쓰는 기간 필터임',
                        '검증 실행·재사용 수 합계는 값이 있는 기록만 포함하며 미기록 수는 별도 표시함',
                        '실행 중 로그는 나중에 usage가 추가되어 같은 기간을 다시 집계해도 값이 달라질 수 있음'],
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"responses={len(records)} advisor_calls={result['total']['advisor_calls']} reviews={sum(q.get('reviewRounds', 0) for q in quality)}")
    print(f"report={args.output} missing_projects={len(missing)} diagnostics={sum(diagnostics.values())}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
