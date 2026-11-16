"""Model Recommendations (모델 선택 추천): measured advice, without changing runtime configuration."""
import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from mission_state import write_text

SCHEMA = 1
STRATA = ('runtime', 'model', 'effort', 'role', 'kind', 'taskKind')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                'cache_creation_input_tokens', 'reasoning_output_tokens', 'total_tokens')


def wilson(successes, samples, z=1.96):
    if not samples:
        return None
    rate = successes / samples
    divisor = 1 + z * z / samples
    center = (rate + z * z / (2 * samples)) / divisor
    margin = z * math.sqrt(rate * (1 - rate) / samples + z * z / (4 * samples * samples)) / divisor
    return [max(0.0, center - margin), min(1.0, center + margin)]


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def explicit_condition(value):
    """Only scalar or flat structured identifiers; no task-name classification."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and value and all(isinstance(k, str) and isinstance(v, (str, int))
                                                 and not isinstance(v, bool) for k, v in value.items()):
        return json.dumps(value, sort_keys=True, separators=(',', ':'))
    return ''


def summary(rows):
    outcomes = [row['success'] for row in rows if type(row.get('success')) is bool]
    successes = sum(outcomes)
    durations = [row['seconds'] for row in rows if number(row.get('seconds'))]
    token_rows = [row['tokens'] for row in rows if isinstance(row.get('tokens'), dict)]
    fields = [key for key in TOKEN_FIELDS if any(key in row for row in token_rows)]
    tokens, coverage = {}, {}
    for field in fields:
        known = [row[field] for row in token_rows if type(row.get(field)) is int and row[field] >= 0]
        tokens[field] = sum(known) if known else None
        coverage[field] = {'known': len(known), 'unknown': len(rows) - len(known)}
    return {'samples': len(rows), 'knownOutcomes': len(outcomes), 'successes': successes,
            'successRate': successes / len(outcomes) if outcomes else None, 'wilson95': wilson(successes, len(outcomes)),
            'meanSeconds': sum(durations) / len(durations) if durations else None,
            'latencyKnown': len(durations), 'tokens': tokens, 'tokenCoverage': coverage,
            'credits': None, 'cost': None}


def grouped(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(key, 'unknown') for key in STRATA)].append(row)
    return [dict(zip(STRATA, key), **summary(values)) for key, values in sorted(groups.items())]


def compare(rows, baseline, min_samples, diagnostics):
    names = sorted({row['strategy'] for row in rows})
    comparisons = []
    if not baseline or baseline not in names:
        diagnostics['missing_explicit_baseline'] += 1
        return comparisons
    for alternative in names:
        if alternative == baseline:
            continue
        left = [row for row in rows if row['strategy'] == baseline]
        right = [row for row in rows if row['strategy'] == alternative]
        left_conditions = {row['condition'] for row in left}
        right_conditions = {row['condition'] for row in right}
        matched = left_conditions & right_conditions
        # Compare strata independently; pooled condition difficulty can confound aggregate rates.
        for condition in sorted(left_conditions | right_conditions):
            base = [row for row in left if row['condition'] == condition]
            other = [row for row in right if row['condition'] == condition]
            a, b = summary(base), summary(other)
            fixture = any(row.get('kind') == 'fixture' for row in [*base, *other])
            status = 'insufficient_evidence'
            if not base or not other:
                status = 'unmatched_condition'
            elif fixture:
                status = 'fixture_only'
            elif min(a['knownOutcomes'], b['knownOutcomes']) < min_samples:
                status = 'insufficient_evidence'
            elif a['knownOutcomes'] != a['samples'] or b['knownOutcomes'] != b['samples']:
                status = 'unknown_outcomes'
            elif b['wilson95'][0] > a['wilson95'][1]:
                status = 'measured_quality_gain'
            elif b['wilson95'][1] < a['wilson95'][0]:
                status = 'measured_regression'
            else:
                status = 'no_demonstrated_improvement'
            efficiency = 'unknown'
            fields = set(a['tokens']) | set(b['tokens'])
            comparable_usage = bool(fields) and all(
                a['tokenCoverage'].get(field, {}).get('known') == a['samples'] > 0 and
                b['tokenCoverage'].get(field, {}).get('known') == b['samples'] > 0 for field in fields)
            if comparable_usage:
                efficiency = 'comparable_observations_only'
            comparisons.append({'baseline': baseline, 'alternative': alternative, 'conditionKey': condition,
                                'matched': condition in matched, 'baselineSummary': a, 'alternativeSummary': b,
                                'status': status, 'fixture': fixture, 'usageComparison': efficiency,
                                'recommendation': 'consider_alternative_for_this_condition' if status == 'measured_quality_gain'
                                                  else 'retain_baseline_pending_evidence'})
    return comparisons


def analyze_records(records, baseline=None, min_samples=5):
    if type(min_samples) is not int or min_samples < 2:
        raise ValueError('min_samples must be at least 2')
    if not isinstance(records, list):
        raise ValueError('records must be a list')
    rows, diagnostics = [], Counter()
    for record in records:
        if not isinstance(record, dict):
            diagnostics['invalid_records'] += 1
            continue
        condition = explicit_condition(record.get('conditionKey'))
        strategy = record.get('strategy')
        if not condition or not isinstance(strategy, str) or not strategy:
            diagnostics['missing_condition_or_strategy'] += 1
            continue
        row = {key: record.get(key) if isinstance(record.get(key), str) else 'unknown' for key in STRATA}
        # Explicit taskKind belongs to matching conditions as well as reporting strata.
        row.update(condition=json.dumps([condition, row['taskKind']], separators=(',', ':')),
                   strategy=strategy, success=record.get('success') if type(record.get('success')) is bool else None,
                   seconds=record.get('seconds') if number(record.get('seconds')) else None,
                   tokens=record.get('tokens') if isinstance(record.get('tokens'), dict) else {})
        if row['success'] is None:
            diagnostics['unknown_outcomes'] += 1
        rows.append(row)
    comparisons = compare(rows, baseline, min_samples, diagnostics)
    return {'schema': SCHEMA, 'mode': 'recommendations_only', 'baseline': baseline,
            'strata': grouped(rows), 'comparisons': comparisons,
            'coverage': {'inputRecords': len(records), 'eligibleRecords': len(rows),
                         'matchedComparisons': sum(row['matched'] for row in comparisons),
                         'unmatchedComparisons': sum(not row['matched'] for row in comparisons)},
            'diagnostics': dict(diagnostics), 'credits': None, 'cost': None,
            'limitations': ['No runtime configuration is changed.', 'Tokens are not credits or provider billing.',
                            'Wilson intervals describe observed candidate outcomes; independence is not established.',
                            'A nonoverlapping interval is evidence for this explicit condition only.',
                            'No claim of general quality gains follows from synthetic or fixture runs.',
                            'Latency is measured evaluation time, not unrecorded development time.']}


def analyze_lab(report, baseline=None, min_samples=5):
    if (not isinstance(report, dict) or type(report.get('schema')) is not int or report['schema'] != 1
            or not isinstance(report.get('results'), list)):
        raise ValueError('Lab schema 1 report required')
    config = report.get('config') if isinstance(report.get('config'), dict) else {}
    baseline = baseline or config.get('baselineStrategy')
    rows, skipped = [], 0
    identifiers = [report.get('evaluator'), report.get('holdoutHash')]
    for candidate in report['results']:
        if not isinstance(candidate, dict):
            skipped += 1
            continue
        strategy = candidate.get('strategy')
        holdout = candidate.get('holdout')
        if (report.get('phase') != 'complete' or not all(isinstance(value, str) and value for value in identifiers)
                or not isinstance(strategy, dict) or not isinstance(holdout, dict)
                or type(holdout.get('passed')) is not int or type(holdout.get('total')) is not int
                or not 0 <= holdout['passed'] <= holdout['total'] or not holdout['total']
                or type(candidate.get('generation')) is not int):
            skipped += 1
            continue
        condition = {'evaluator': identifiers[0], 'holdoutHash': identifiers[1],
                     'generation': candidate['generation']}
        # Additional explicit condition differentiates tasks; names never imply difficulty.
        if 'conditionKey' in candidate:
            explicit = explicit_condition(candidate['conditionKey'])
            if not explicit:
                skipped += 1
                continue
            condition['condition'] = explicit
        rows.append({**{key: strategy.get(key, 'unknown') for key in STRATA},
                     'taskKind': candidate.get('taskKind', config.get('taskKind', 'synthetic_interval_union')),
                     'strategy': strategy.get('name'), 'conditionKey': condition,
                     'success': holdout['passed'] == holdout['total'], 'seconds': holdout.get('seconds'),
                     'tokens': candidate.get('tokens', {})})
    result = analyze_records(rows, baseline, min_samples)
    result['source'] = 'lab_completed_holdout'
    result['modelMetadataBasis'] = 'configured_strategy_not_observed_runtime'
    result['limitations'].append('Lab strategy model/effort labels are configuration metadata; actual runtime application is unverified.')
    result['coverage']['labCandidates'] = len(report['results'])
    result['coverage']['excludedCandidates'] = skipped
    result['limitations'].append('One candidate artifact is one outcome; holdout cases are not independent agent trials.')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lab-report', type=Path)
    parser.add_argument('--run-metrics-report', '--observatory-report', dest='observatory_report', type=Path)
    parser.add_argument('--baseline')
    parser.add_argument('--min-samples', type=int, default=5)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if bool(args.lab_report) == bool(args.observatory_report):
        parser.error('select one explicit --lab-report or --observatory-report')
    try:
        source = json.loads((args.lab_report or args.observatory_report).read_text(encoding='utf-8'))
        result = (analyze_lab(source, args.baseline, args.min_samples) if args.lab_report else
                  analyze_records(source.get('records'), args.baseline, args.min_samples))
        write_text(str(args.output), json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
