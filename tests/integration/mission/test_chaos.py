from pathlib import Path

from tests.paths import PLUGIN_ROOT

"""Bounded, synthetic fault injection; no runtime CLI, adapter registry or server.

Workers only import local Python modules and write under pytest's temporary root.
They never spawn descendants; the fixture reaps every worker even on assertion failure.
Resource locks deliberately require operator recovery after a killed holder.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.transition_support import run_lifecycle_hook
from chef.mission import state as mission_state
from chef.verification import runner as verification
from test_verification import (  # Reuse synthetic mission helpers.
    mission,
    review,
    verify,
)

slots = verification.locking
PLUGIN = PLUGIN_ROOT

WORKER = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import verification
mode, directory, label = sys.argv[2:]
root = Path(directory)
lock_type = verification.ResourceLock if mode == 'resource' else verification.locking.FileLock
if mode == 'counter':
    (root / (label + '.ready')).touch()
    deadline = time.monotonic() + 15
    while not (root / 'go').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('parent never released start gate')
        time.sleep(.01)
    for _ in range(8):
        with lock_type(str(root / 'shared.lock'), 'counter', timeout=15):
            counter = root / 'counter'
            value = int(counter.read_text()) if counter.exists() else 0
            with (root / 'events').open('a') as log:
                log.write(f'enter {label}\n')
                log.flush()
                time.sleep(.005)
                counter.write_text(str(value + 1))
                log.write(f'exit {label}\n')
else:
    with lock_type(str(root / 'shared.lock'), 'holder'):
        (root / (label + '.ready')).touch()
        time.sleep(30)
"""


@pytest.fixture(autouse=True)
def isolated_chaos_environment(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    for key in ('HOME', 'USERPROFILE', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'GROK_HOME',
                'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(key, str(home / key.lower()))
    for key in ('CLAUDE_PROJECT_DIR', 'CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR', 'HAN_REPO',
                'GROK_HOOK_EVENT', 'GROK_SESSION_ID', 'GROK_WORKSPACE_ROOT', 'HAN_GUARD_LOG'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HAN_ADAPTERS_DIR', str(tmp_path / 'empty-registry'))
    monkeypatch.setenv('PYTHONUTF8', '1')
    monkeypatch.setenv('PYTHONIOENCODING', 'utf-8')
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def workers(tmp_path):
    children = []

    def start(mode, label):
        process = subprocess.Popen(
            [sys.executable, '-B', '-c', WORKER, str((PLUGIN_ROOT / 'skills/mission/scripts')), mode, str(tmp_path), label],
            cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        children.append(process)
        return process

    try:
        yield start
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def await_ready(tmp_path, labels, children):
    deadline = time.monotonic() + 15
    while not all((tmp_path / (label + '.ready')).exists() for label in labels):
        assert all(child.poll() is None for child in children), 'worker exited before readiness'
        assert time.monotonic() < deadline, 'worker readiness timed out'
        time.sleep(.01)


def test_real_process_contention_preserves_critical_sections(tmp_path, workers):
    labels = ['one', 'two', 'three', 'four']
    children = [workers('counter', label) for label in labels]
    await_ready(tmp_path, labels, children)
    (tmp_path / 'go').touch()
    for child in children:
        _, errors = child.communicate(timeout=25)
        assert child.returncode == 0, errors.decode('utf-8', errors='replace')
    assert (tmp_path / 'counter').read_text() == '32'
    events = (tmp_path / 'events').read_text().splitlines()
    assert len(events) == 64
    for enter, exit_ in zip(events[::2], events[1::2]):
        assert enter.startswith('enter ')
        assert exit_ == enter.replace('enter ', 'exit ', 1)
    assert not (tmp_path / 'shared.lock').exists()


@pytest.mark.parametrize('mode', ['slot', 'resource'])
def test_killed_lock_holder_has_explicit_recovery_contract(tmp_path, workers, mode):
    child = workers(mode, 'holder')
    await_ready(tmp_path, ['holder'], [child])
    path = tmp_path / 'shared.lock'
    lock_type = verification.ResourceLock if mode == 'resource' else slots.FileLock
    original = path.read_bytes()
    with pytest.raises(slots.LockBusy):
        with lock_type(str(path), 'live contender'):
            pytest.fail('a live holder must exclude contenders')
    assert path.read_bytes() == original
    child.kill()
    child.communicate(timeout=10)
    assert not slots.process_alive(child.pid)
    if mode == 'resource':
        # Killing the Python owner says nothing about an external editor's lifetime.
        # Preserve the stale evidence until an operator has checked that resource.
        with pytest.raises(verification.VerificationError) as failure:
            with verification.resource_locks({'resourceDir': str(tmp_path), 'resources': ['shared']}):
                pytest.fail('resource ownership must fail closed')
        assert failure.value.category == 'resource_busy'
        assert path.read_bytes() == original
        path.unlink()  # Simulated operator recovery, only inside this synthetic root.
    with lock_type(str(path), 'recovered'):
        assert path.read_text().startswith(str(os.getpid()) + '\t')
    assert not path.exists()


@pytest.mark.parametrize('damage', ['header', 'truncated-row'])
def test_corrupt_mission_state_blocks_transition_without_overwrite(mission, damage):
    _, folder, _ = mission
    path = folder / 'units.tsv'
    if damage == 'header':
        path.write_text('unrecognized\n', encoding='utf-8')
    else:
        path.write_text(path.read_text(encoding='utf-8').splitlines()[0] + '\nM1\tbroken\n', encoding='utf-8')
    corrupted = path.read_bytes()
    assert mission_state.main(['unit', 'set', 'M1', '--goal', 'Updated synthetic goal']) != 0
    assert path.read_bytes() == corrupted


@pytest.mark.parametrize('damage', ['json', 'status', 'snapshot', 'logHash', 'steps'])
def test_damaged_receipt_cannot_complete_or_write_pass_ledger(mission, damage):
    root, folder, _ = mission
    code, receipt = verify(mission)
    assert code == 0
    clean_review = review(mission, receipt)
    value = json.loads(receipt.read_text(encoding='utf-8'))
    verification.validate_receipt(root, folder, 'M1', value,
                                  settings={'grades': ['pytest-pass', 'editmode-pass']})
    verification.validate_review(folder, 'M1', receipt, clean_review)
    if damage == 'json':
        receipt.write_text('{"status":', encoding='utf-8')
    else:
        value[damage] = {'status': 'fail', 'snapshot': {}, 'logHash': 'tampered', 'steps': []}[damage]
        receipt.write_text(json.dumps(value), encoding='utf-8')
    ledger = folder / 'ledger.tsv'
    before = ledger.read_bytes()
    assert mission_state.main(['complete', 'M1', '--receipt', str(receipt),
                               '--review', str(clean_review), '--done-evidence', 'Synthetic check passed']) != 0
    assert ledger.read_bytes() == before


@pytest.mark.parametrize('runtime', ['claude', 'codex', 'grok'])
def test_runtime_guard_owner_and_child_contract_in_real_hook_process(tmp_path, runtime):
    root = tmp_path / 'fictional-project'
    adapter = root / f'.{runtime}/orchestrator/adapter.json'
    adapter.parent.mkdir(parents=True)
    adapter.write_text(json.dumps({'schema': 1, 'paths': {'stateDir': 'state'},
                                   'guard': {'deny': [{'pattern': r'^p4\s+submit\b', 'reason': 'blocked'}]}}),
                       encoding='utf-8')
    script = PLUGIN / ('codex/mission_guard.py' if runtime == 'codex' else 'hooks/mission_guard.py')

    def hook(command, session='owner', child=False):
        payload = {'cwd': str(root)}
        if runtime == 'grok':
            payload.update(hookEventName='pre_tool_use', sessionId=session,
                           toolName='run_terminal_command', toolInput={'command': command, 'shell': 'bash'})
            if child:
                payload['subagentType'] = 'developer'
        else:
            payload.update(hook_event_name='PreToolUse', session_id=session,
                           tool_name='Bash', tool_input={'command': command})
            if child:
                payload.update(agent_id='child', agent_type='developer')
        result = run_lifecycle_hook(script, payload, cwd=root, timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout or b'{}')

    def denied(value):
        return value.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'

    assert not denied(hook('echo han-mission open garden'))
    active = root / 'state/ACTIVE'
    assert json.loads(active.read_text(encoding='utf-8'))['session'] == 'owner'
    assert denied(hook('p4 submit -c 123'))
    # Claude/Codex children carry the parent's session; Grok has a native child session.
    child_session = 'child' if runtime == 'grok' else 'owner'
    assert denied(hook('p4 submit -c 123', session=child_session, child=True))
    assert not denied(hook('p4 opened -c 123'))
    assert not denied(hook('p4 submit -c 123', session='unrelated'))
    before = active.read_bytes()
    hook('echo han-mission close garden', session='unrelated')
    assert active.read_bytes() == before
    if runtime == 'grok':
        # Grok identifies a native child independently of the owner session.
        assert denied(hook('echo han-mission close garden', session=child_session, child=True))
        assert active.read_bytes() == before
    assert not denied(hook('echo han-mission close garden'))
    assert not active.exists()
