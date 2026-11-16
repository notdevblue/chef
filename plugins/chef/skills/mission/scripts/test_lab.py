"""Synthetic coordinator and actual Linux sandbox tests; no live model providers."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import lab


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for key in ('HOME', 'USERPROFILE', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'GROK_HOME',
                'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'HAN_ADAPTERS_DIR'):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    for key in ('CLAUDE_PROJECT_DIR', 'HAN_REPO', 'CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR',
                'GROK_HOOK_EVENT', 'GROK_SESSION_ID'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def configuration(generations=1, budget=None):
    return {'schema': 1, 'generations': generations, 'candidates': 4,
            'maxEvaluations': budget if budget is not None else generations * 8,
            'maxSeconds': 120, 'maxConcurrent': 4, 'seed': 17,
            'costPolicy': 'unknown-no-paid-launches',
            'strategies': [{'name': f'fictional-{i}', 'kind': 'fixture', 'runtime': 'fixture',
                            'model': 'none', 'effort': 'none'} for i in range(4)]}


def fill(root, generation):
    for index, name in enumerate(lab.slot_names(configuration(), generation)):
        (root / 'slots' / name / 'solution.py').write_text(str(index), encoding='utf-8')


def scorer(path, inputs):
    index = int(path.read_text(encoding='utf-8'))
    # Development favors c03; holdout deliberately favors the opposite candidate.
    passed = index + 1 if len(inputs) == 26 else 4 - index
    return {'status': 'scored', 'passed': passed, 'total': len(inputs), 'seconds': .01}


def test_prepare_reuses_novcs_slots_and_separates_hidden_cases(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('preparing NoVcs slots must not execute VCS/runtime commands')
    monkeypatch.setattr(lab.slots.subprocess, 'run', forbidden)
    root = tmp_path / 'experiment'
    state = lab.prepare(root, configuration())
    assert state['phase'] == 'prepared'
    assert lab.load(root)[1] == state
    adapter = lab.read(root / 'adapter.json')
    assert adapter['slots']['vcs'] == 'none'
    hidden = lab.read(root / 'private/holdout.json')
    assert lab.hashed(hidden['cases']) == state['holdoutHash']
    for name in lab.slot_names(configuration(), 1):
        folder = root / 'slots' / name
        assert (folder / 'TASK.md').read_text(encoding='utf-8') == lab.SPEC
        public = lab.read(folder / 'public.json')
        assert len(public) == 26
        assert all(row['expected'] == lab.oracle(row['input']) for row in public)
        assert not (folder / 'holdout.json').exists()
        assert not (folder / 'private').exists()
        assert set(folder.rglob('*.json')) == {folder / 'public.json', folder / 'strategy.json'}
    assert not (root / 'results/released-holdout.json').exists()
    with pytest.raises(lab.LabError):
        lab.prepare(root, configuration())


@pytest.mark.parametrize('field,value', [
    ('schema', 2), ('schema', True), ('schema', 1.0), ('generations', 0), ('generations', 11), ('generations', True),
    ('candidates', 3), ('candidates', 21), ('maxEvaluations', 7),
    ('maxSeconds', 0), ('maxConcurrent', 0), ('seed', -1), ('seed', True),
    ('costPolicy', 'paid'), ('strategies', []),
])
def test_config_rejects_invalid_contract(field, value):
    config = configuration()
    config[field] = value
    with pytest.raises(lab.LabError):
        lab.config_check(config)


@pytest.mark.parametrize('damage', ['path', 'missing-field', 'extra-field', 'bad-kind'])
def test_strategy_metadata_is_explicit_and_safe(damage):
    config = configuration()
    item = config['strategies'][1]
    if damage == 'path':
        item['name'] = '../escape'
    elif damage == 'missing-field':
        item.pop('model')
    elif damage == 'extra-field':
        item['command'] = 'live-provider'
    else:
        item['kind'] = 'paid'
    with pytest.raises(lab.LabError):
        lab.config_check(config)


def test_replicated_strategies_keep_independent_candidate_identities(tmp_path):
    config = configuration()
    config['strategies'][1] = dict(config['strategies'][0])
    root = tmp_path / 'replicated-experiment'
    lab.prepare(root, config)
    names = lab.slot_names(config, 1)
    assert len(set(names)) == config['candidates']
    assert lab.read(root / 'slots' / names[0] / 'strategy.json') == lab.read(root / 'slots' / names[1] / 'strategy.json')
    fill(root, 1)
    result = lab.evaluate(root, scorer)
    assert len(result['results']) == 4
    assert len({row['id'] for row in result['results']}) == 4
    config['strategies'][1]['model'] = 'different-settings'
    with pytest.raises(lab.LabError, match='identical settings'):
        lab.config_check(config)


def test_five_generations_reproducible_and_selection_excludes_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(lab.os, 'urandom', lambda length: b'x' * length)
    reports = []
    for label in ('first', 'reproduction'):
        root = tmp_path / label
        lab.prepare(root, configuration(5))
        for generation in range(1, 6):
            fill(root, generation)
            if generation > 1:
                for name in lab.slot_names(configuration(), generation):
                    assert (root / 'slots' / name / 'parent.py').read_text() == '3'
            result = lab.evaluate(root, scorer)
            if generation < 5:
                assert all('holdout' not in row for row in result['results'])
                assert not (root / 'results/released-holdout.json').exists()
        assert result['phase'] == 'complete'
        assert result['evaluations'] == 40
        assert result['selections'] == [f'g{generation:02d}-c03' for generation in range(1, 6)]
        assert len(result['results']) == 20
        assert all(row['holdout']['passed'] == 4 - int(row['id'][-2:]) for row in result['results'])
        assert lab.read(root / 'results/report.json') == result
        released = lab.read(root / 'results/released-holdout.json')
        assert released['seed'] == lab.load(root)[2]['seed']
        assert lab.cases(released['seed'], released['randomCases']) == lab.load(root)[2]['cases']
        assert lab.replay(root / 'results', scorer)['reproduced'] is True
        assert result['cost'] is None and result['credits'] is None
        # Root paths and real elapsed coordinator time are intentionally not reproducible.
        result.pop('elapsedSeconds')
        reports.append(result)
    assert reports[0] == reports[1]


def test_stop_resume_and_pending_recovery_preserve_consumed_budget(tmp_path):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration(budget=8))
    fill(root, 1)
    assert lab.control(root, 'stop')['stopped']
    with pytest.raises(lab.LabError, match='stopped'):
        lab.evaluate(root, scorer)
    assert lab.load(root)[1]['evaluations'] == 0
    assert not lab.control(root, 'resume')['stopped']
    def crash(*args):
        raise RuntimeError('synthetic evaluator crash')
    with pytest.raises(RuntimeError, match='synthetic'):
        lab.evaluate(root, crash)
    state = lab.load(root)[1]
    assert state['evaluations'] == 1 and state['pending']['split'] == 'development'
    with pytest.raises(lab.LabError, match='recover'):
        lab.evaluate(root, scorer)
    recovered = lab.control(root, 'recover')
    assert recovered['evaluations'] == 1
    assert lab.load(root)[1]['pending'] is None
    with pytest.raises(lab.LabError, match='budget'):
        lab.evaluate(root, scorer)
    state = lab.load(root)[1]
    assert state['evaluations'] == 8
    assert state['phase'] != 'complete'
    assert not (root / 'results/released-holdout.json').exists()


def test_holdout_crash_resume_does_not_repeat_selection_or_finished_scores(tmp_path):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration(budget=12))
    fill(root, 1)
    calls = []
    def crash(path, inputs):
        calls.append((path.name, len(inputs)))
        if len(calls) == 6:
            raise RuntimeError('holdout crash')
        return scorer(path, inputs)
    with pytest.raises(RuntimeError, match='holdout crash'):
        lab.evaluate(root, crash)
    before = lab.load(root)[1]
    assert before['phase'] == 'holdout' and before['selections'] == ['g01-c03']
    assert 'holdout' in before['results'][0]
    assert all('holdout' not in row for row in lab.report(root)['results'])
    lab.control(root, 'recover')
    completed = []
    def resume(path, inputs):
        completed.append(path.name)
        return scorer(path, inputs)
    result = lab.evaluate(root, resume)
    assert result['phase'] == 'complete'
    assert result['selections'] == ['g01-c03']
    assert result['evaluations'] == 9
    assert completed == ['g01-c01.py', 'g01-c02.py', 'g01-c03.py']


def test_candidates_frozen_before_any_scoring_and_changes_rejected_on_resume(tmp_path):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration(budget=12))
    fill(root, 1)
    def crash(path, inputs):
        assert all((root / 'snapshots' / f'{name}.py').exists()
                   for name in lab.slot_names(configuration(), 1))
        raise RuntimeError('crash after freeze')
    with pytest.raises(RuntimeError):
        lab.evaluate(root, crash)
    snapshot = root / 'snapshots/g01-c03.py'
    frozen = snapshot.read_bytes()
    (root / 'slots/g01-c03/solution.py').write_text('999')
    lab.control(root, 'recover')
    with pytest.raises(lab.LabError, match='frozen'):
        lab.evaluate(root, scorer)
    assert snapshot.read_bytes() == frozen


@pytest.mark.parametrize('field,value', [('phase', 'unknown'), ('elapsedSeconds', -1),
                                        ('elapsedSeconds', 'bad'), ('pending', []),
                                        ('results', [None]), ('selections', ['../escape']),
                                        ('phase', 'complete'), ('phase', 'holdout'),
                                        ('evaluations', 999),
                                        ('pending', {'id': '../escape', 'split': 'development', 'startedAt': 1})])
def test_malformed_state_fails_closed_without_mutation(tmp_path, field, value):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration())
    path = root / 'state.json'
    state = lab.read(path)
    state[field] = value
    lab.save(path, state)
    before = path.read_bytes()
    with pytest.raises(lab.LabError):
        lab.load(root)
    assert path.read_bytes() == before


def test_final_publication_crash_can_retry_without_any_rescoring(tmp_path, monkeypatch):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration())
    fill(root, 1)
    original_save = lab.save
    def interrupt(path, value):
        if Path(path) == root / 'results/report.json':
            raise OSError('synthetic final publication failure')
        return original_save(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(lab, 'save', interrupt)
        with pytest.raises(OSError, match='publication failure'):
            lab.evaluate(root, scorer)
    assert lab.load(root)[1]['phase'] == 'complete'
    def forbidden(*args):
        pytest.fail('completed experiment must republish without scoring')
    result = lab.evaluate(root, forbidden)
    assert result['evaluations'] == 8
    assert lab.read(root / 'results/report.json') == result
    assert (root / 'results/released-holdout.json').is_file()


def test_saved_candidate_hash_detects_snapshot_tampering(tmp_path):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration(budget=12))
    fill(root, 1)
    calls = []
    def crash(path, inputs):
        calls.append(path.name)
        if len(calls) == 2:
            raise RuntimeError('synthetic partial development crash')
        return scorer(path, inputs)
    with pytest.raises(RuntimeError):
        lab.evaluate(root, crash)
    (root / 'snapshots/g01-c00.py').write_text('999')
    with pytest.raises(lab.LabError, match='integrity'):
        lab.load(root)


@pytest.mark.parametrize('field,value', [('passed', -1), ('total', 0), ('seconds', float('nan')),
                                        ('status', 'pass'), ('passed', True)])
def test_invalid_scorer_result_preserves_pending_consumed_attempt(tmp_path, field, value):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration())
    fill(root, 1)
    def invalid(path, inputs):
        result = scorer(path, inputs)
        result[field] = value
        return result
    with pytest.raises(lab.LabError, match='scorer'):
        lab.evaluate(root, invalid)
    state = lab.load(root)[1]
    assert state['evaluations'] == 1
    assert state['pending'] is not None
    assert not state['results']


@pytest.fixture
def bubblewrap():
    if sys.platform != 'linux' or not shutil.which('bwrap'):
        pytest.skip('Linux bubblewrap is unavailable; unsafe execution is never substituted')
    probe = subprocess.run(['bwrap', '--unshare-all', '--ro-bind', '/usr', '/usr',
                            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
                            '/usr/bin/true'], capture_output=True, timeout=10)
    if probe.returncode:
        message = probe.stderr.decode(errors='replace')
        if any(reason in message.lower() for reason in ('operation not permitted', 'no permissions',
                                                        'creating new namespace failed')):
            pytest.skip('kernel/runner does not permit bubblewrap namespaces: ' + message.strip())
        pytest.fail('unexpected bubblewrap probe failure: ' + message)


def test_real_bubblewrap_blocks_host_holdout_environment_network_and_writes(tmp_path, monkeypatch, bubblewrap):
    secret = tmp_path / 'private-host-file'
    secret.write_text('synthetic secret')
    hidden = tmp_path / 'private/holdout.json'
    hidden.parent.mkdir()
    hidden.write_text('synthetic holdout')
    monkeypatch.setenv('CHEF_SYNTHETIC_SECRET', 'must not reach candidate')
    code = tmp_path / 'solution.py'
    code.write_text('import os, socket\n'
                    'def solve(intervals):\n'
                    '    assert "CHEF_SYNTHETIC_SECRET" not in os.environ\n'
                    f'    for path in ({str(secret)!r}, {str(hidden)!r}):\n'
                    '        try:\n'
                    '            open(path).read()\n'
                    '        except OSError:\n'
                    '            pass\n'
                    '        else:\n'
                    '            raise AssertionError("host file readable")\n'
                    '    try:\n'
                    '        open("/candidate/solution.py", "w").write("changed")\n'
                    '    except OSError:\n'
                    '        pass\n'
                    '    else:\n'
                    '        raise AssertionError("snapshot writable")\n'
                    '    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:\n'
                    '        try:\n'
                    '            connection.connect(("192.0.2.1", 9))\n'
                    '        except OSError:\n'
                    '            pass\n'
                    '        else:\n'
                    '            raise AssertionError("network route available")\n'
                    '    return []\n', encoding='utf-8')
    before = code.read_bytes()
    result = lab.isolated_score(code, [[]])
    assert result['status'] == 'scored' and result['passed'] == 1
    assert code.read_bytes() == before
    assert secret.read_text() == 'synthetic secret'


def test_real_bubblewrap_runaway_candidate_is_bounded(tmp_path, bubblewrap):
    code = tmp_path / 'solution.py'
    code.write_text('def solve(intervals):\n    while True:\n        pass\n')
    result = lab.isolated_score(code, [[]], timeout=.2)
    assert result['status'] == 'timeout' and result['passed'] == 0
    assert result['seconds'] < 5


def test_real_bubblewrap_scores_solution_and_rejects_input_mutation(tmp_path, bubblewrap):
    code = tmp_path / 'solution.py'
    implementation = ('def solve(intervals):\n'
                      '    result = []\n'
                      '    for start, end in sorted(intervals):\n'
                      '        if result and start <= result[-1][1]:\n'
                      '            result[-1][1] = max(end, result[-1][1])\n'
                      '        else:\n'
                      '            result.append([start, end])\n'
                      '    return result\n')
    inputs = lab.cases(27, 20)
    code.write_text(implementation)
    result = lab.isolated_score(code, inputs)
    assert result['status'] == 'scored' and result['passed'] == result['total'] == len(inputs)
    # The answers still match, but mutating the supplied list disqualifies the case.
    code.write_text(implementation.replace('    return result', '    intervals.clear()\n    return result'))
    result = lab.isolated_score(code, [[[1, 2], [2, 4]]])
    assert result['status'] == 'scored' and result['passed'] == 0


def test_unsupported_sandbox_has_no_unisolated_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(lab.shutil, 'which', lambda name: None)
    code = tmp_path / 'solution.py'
    code.write_text('raise RuntimeError("must never execute")')
    with pytest.raises(lab.LabError, match='bubblewrap'):
        lab.isolated_score(code, [[]])


PUBLISHED = Path(__file__).resolve().parents[5] / 'examples/lab/interval-union'


def published_tree(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_generated_publication_replays_without_manual_assembly(tmp_path):
    root = tmp_path / 'experiment'
    lab.prepare(root, configuration())
    fill(root, 1)
    result = lab.evaluate(root, scorer)
    assert result['phase'] == 'complete'
    before = published_tree(root / 'results')
    assert lab.replay(root / 'results', scorer)['reproduced'] is True
    assert published_tree(root / 'results') == before


def test_published_fixture_replay_checks_released_seed_hashes_and_is_readonly():
    before = published_tree(PUBLISHED)
    published = lab.read(PUBLISHED / 'report.json')
    config = lab.read(PUBLISHED / 'config.json')
    released = lab.read(PUBLISHED / 'released-holdout.json')
    assert published['config'] == config
    assert published['configHash'] == lab.hashed(config)
    inputs = lab.cases(released['seed'], 100)
    assert published['holdoutHash'] == lab.hashed(inputs)
    expected = {row['id']: row for row in published['results']}
    seen = []
    def fake_score(path, inputs):
        row = expected[path.stem]
        assert inputs == lab.cases(released['seed'], 100)
        assert lab.hashed(path.read_bytes()) == row['codeHash']
        seen.append(path.stem)
        return {key: row['holdout'][key] for key in ('status', 'passed', 'total', 'seconds')}
    replay = lab.replay(PUBLISHED, fake_score)
    assert replay['reproduced'] is True
    assert replay['mode'] == 'released_data_replay'
    assert seen == list(expected)
    assert replay['cost'] is None and replay['credits'] is None
    assert published_tree(PUBLISHED) == before


@pytest.mark.parametrize('damage', ['seed', 'code-hash', 'traversal', 'empty-results', 'config-hash'])
def test_replay_rejects_damaged_publication_before_running_candidate(tmp_path, damage):
    root = tmp_path / 'publication'
    shutil.copytree(PUBLISHED, root)
    report = lab.read(root / 'report.json')
    if damage == 'seed':
        released = lab.read(root / 'released-holdout.json')
        released['seed'] += 1
        lab.save(root / 'released-holdout.json', released)
    elif damage == 'code-hash':
        candidate = root / 'candidates' / f"{report['results'][0]['id']}.py"
        candidate.write_bytes(candidate.read_bytes() + b'\n# changed\n')
    elif damage == 'traversal':
        report['results'][0]['id'] = '../outside'
    elif damage == 'empty-results':
        report['results'] = []
    else:
        report['configHash'] = 'invalid'
    lab.save(root / 'report.json', report)
    before = published_tree(root)
    def forbidden(*args):
        pytest.fail('damaged publication must fail before candidate execution')
    with pytest.raises(lab.LabError):
        lab.replay(root, forbidden)
    assert published_tree(root) == before


def test_real_bubblewrap_reproduces_published_fixture_without_mutation(bubblewrap):
    before = published_tree(PUBLISHED)
    replay = lab.replay(PUBLISHED)
    assert replay['reproduced'] is True
    assert len(replay['results']) == 4
    assert all(row['matchesPublishedScore'] for row in replay['results'])
    assert published_tree(PUBLISHED) == before


@pytest.mark.parametrize('answer', ['[[True, 2]]', '[[1, 2.0]]'])
def test_real_bubblewrap_rejects_noninteger_interval_answers(tmp_path, bubblewrap, answer):
    code = tmp_path / 'solution.py'
    code.write_text(f'def solve(intervals):\n    return {answer}\n')
    result = lab.isolated_score(code, [[[1, 2]]])
    assert result['status'] == 'scored'
    assert result['passed'] == 0
