from pathlib import Path

from chef.mission import routing as routing

from tests.paths import PLUGIN_ROOT


def row(strategy, success, condition='same', **kwargs):
    return {'strategy': strategy, 'success': success, 'conditionKey': condition,
            'runtime': 'codex', 'model': 'synthetic', 'kind': 'single', 'taskKind': 'implementation', **kwargs}


def test_unknown_observatory_outcomes_do_not_imply_success():
    result = routing.analyze_records([row('base', None), row('other', None)], 'base')
    assert result['comparisons'][0]['status'] == 'insufficient_evidence'
    assert result['strata'][0]['successRate'] is None
    assert result['diagnostics']['unknown_outcomes'] == 2


def test_matched_quality_gain_requires_enough_candidate_trials():
    rows = [row('base', False) for _ in range(40)] + [row('other', True) for _ in range(40)]
    result = routing.analyze_records(rows, 'base')
    assert result['comparisons'][0]['status'] == 'measured_quality_gain'
    assert result['comparisons'][0]['usageComparison'] == 'unknown'
    assert result['credits'] is None


def test_conditions_and_task_kind_prevent_confounded_pooling():
    rows = [row('base', False, 'hard') for _ in range(40)] + [row('other', True, 'easy') for _ in range(40)]
    result = routing.analyze_records(rows, 'base')
    assert all(item['status'] == 'unmatched_condition' for item in result['comparisons'])
    result = routing.analyze_records([row('base', True), row('other', True, taskKind='review')], 'base')
    assert result['coverage']['matchedComparisons'] == 0


def test_fixtures_low_n_and_no_improvement_are_honest():
    result = routing.analyze_records([row('base', False), row('other', True)], 'base')
    assert result['comparisons'][0]['status'] == 'insufficient_evidence'
    rows = [row('base', False) for _ in range(40)] + [row('other', True, kind='fixture') for _ in range(40)]
    assert routing.analyze_records(rows, 'base')['comparisons'][0]['status'] == 'fixture_only'
    rows = [row(name, True) for name in ('base', 'other') for _ in range(40)]
    assert routing.analyze_records(rows, 'base')['comparisons'][0]['status'] == 'no_demonstrated_improvement'


def test_partial_token_coverage_prevents_efficiency_comparison():
    rows = [row('base', True, tokens={'input_tokens': 10}), row('other', True, tokens={})]
    assert routing.analyze_records(rows, 'base')['comparisons'][0]['usageComparison'] == 'unknown'


def test_lab_uses_holdout_only_and_candidate_count_not_case_count():
    def candidate(name, passed=100):
        return {'generation': 1, 'strategy': {'name': name, 'kind': 'fixture', 'runtime': 'unknown',
                                             'model': 'unknown', 'effort': 'unknown'},
                'passed': 0, 'total': 100, 'holdout': {'passed': passed, 'total': 100, 'seconds': .1}}
    lab = {'schema': 1, 'phase': 'complete', 'evaluator': 'test-v1', 'holdoutHash': 'same',
           'results': [candidate('base'), candidate('other')], 'config': {}}
    result = routing.analyze_lab(lab, 'base')
    assert result['comparisons'][0]['baselineSummary']['knownOutcomes'] == 1
    assert result['comparisons'][0]['baselineSummary']['successRate'] == 1
    assert result['comparisons'][0]['status'] == 'fixture_only'
    lab['phase'] = 'prepared'
    assert routing.analyze_lab(lab, 'base')['coverage']['excludedCandidates'] == 2


def test_missing_explicit_baseline_or_condition_does_not_guess():
    result = routing.analyze_records([row('easy-single', True)])
    assert result['comparisons'] == []
    assert result['diagnostics']['missing_explicit_baseline'] == 1
    result = routing.analyze_records([{'strategy': 'single', 'success': True}])
    assert result['coverage']['eligibleRecords'] == 0


def test_replicated_lab_strategy_runs_can_reach_recommendation_threshold():
    candidates = []
    for name, passed in (('base', 0), ('other', 100)):
        for _ in range(5):
            candidates.append({'generation': 1, 'strategy': {'name': name, 'kind': 'single',
                'runtime': 'codex', 'model': 'configured-only', 'effort': 'medium'},
                'holdout': {'passed': passed, 'total': 100, 'seconds': .1}})
    report = {'schema': 1, 'phase': 'complete', 'evaluator': 'synthetic-test',
              'holdoutHash': 'same-condition', 'results': candidates, 'config': {}}
    result = routing.analyze_lab(report, 'base')
    assert result['comparisons'][0]['status'] == 'measured_quality_gain'
    assert result['comparisons'][0]['baselineSummary']['knownOutcomes'] == 5
    assert result['modelMetadataBasis'] == 'configured_strategy_not_observed_runtime'
