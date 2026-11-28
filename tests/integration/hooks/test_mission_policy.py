"""External mission policy failures must precede lifecycle state changes."""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from chef.cli import mission_entry
from chef.guards import mission, policy
from chef.runtimes.hermes import workers


def execute_marker(value):
    # Test commit-time policy and lock semantics; hook/CLI separation has its own integration tests.
    return mission.decide(value, marker_mode="execute")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "fictional-garden"
    root.mkdir()
    adapter = root / ".claude/orchestrator/adapter.json"
    adapter.parent.mkdir(parents=True)
    active = root / "state/ACTIVE"
    active.parent.mkdir()
    original = {"id": "orchard", "session": "gardener-a", "runtime": "claude"}
    active.write_text(json.dumps(original), encoding="utf-8")
    for key in tuple(os.environ):
        if key.startswith(("CHEF_", "HAN_", "CLAUDE_", "GROK_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return root, adapter, active


def configure(project, argv=None):
    root, adapter, active = project
    data = {"schema": 1, "paths": {"stateDir": "state"}}
    if argv is not None:
        data["guard"] = {"missionPolicyCommand": argv}
    adapter.write_text(json.dumps(data), encoding="utf-8")


def payload(project, runtime="claude", session="gardener-b", action="resume", **extra):
    return {"cwd": str(project[0]), "han_runtime": runtime, "session_id": session,
        "tool_name": "Bash", "tool_input": {"command": f"echo han-mission {action} orchard"}, **extra}


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_unconfigured_resume_keeps_existing_takeover_behavior(project, runtime):
    configure(project)
    result = execute_marker(payload(project, runtime))
    assert not result.deny
    assert json.loads(project[2].read_text())["session"] == "gardener-b"


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_child_markers_do_not_change_active(project, runtime):
    configure(project)
    original = project[2].read_bytes()
    assert execute_marker(payload(project, runtime, agent_id="seedling")).deny
    assert project[2].read_bytes() == original


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("code", [
    "print('not-json')", "print('{}')", "print('[]')",
    "import json; print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'fictional restriction'}}))",
    "import sys; sys.exit(4)", "print('x'*65537)",
])
def test_external_policy_failures_preserve_active(project, runtime, code):
    configure(project, [sys.executable, "-c", code])
    original = project[2].read_bytes()
    assert execute_marker(payload(project, runtime)).deny
    assert project[2].read_bytes() == original


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("code", ["pass", "import json; print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'allow'}}))"])
def test_successful_policy_allows_transition(project, runtime, code):
    configure(project, [sys.executable, "-c", code])
    assert not execute_marker(payload(project, runtime)).deny
    assert json.loads(project[2].read_text())["session"] == "gardener-b"


@pytest.mark.parametrize("argv", [[], None, "not-argv", [""], ["fictional-missing-executable"]])
def test_invalid_configured_policy_denies(project, argv):
    configure(project)
    data = json.loads(project[1].read_text())
    data["guard"] = {"missionPolicyCommand": argv}
    project[1].write_text(json.dumps(data), encoding="utf-8")
    original = project[2].read_bytes()
    assert execute_marker(payload(project)).deny
    assert project[2].read_bytes() == original


def test_timeout_and_stderr_limit_are_fail_closed(project):
    assert policy.command_denial([sys.executable, "-c", "import time; time.sleep(5)"], {}, str(project[0]), timeout=.1)
    assert policy.command_denial([sys.executable, "-c", "import sys; sys.stderr.write('x'*65537)"], {}, str(project[0]))


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_close_policy_and_timeout_preserve_active(project, runtime, monkeypatch):
    configure(project, [sys.executable, "-c", "import time; time.sleep(5)"])
    monkeypatch.setattr(policy, "POLICY_TIMEOUT", .1)
    original = project[2].read_bytes()
    assert execute_marker(payload(project, runtime, session="gardener-a", action="close")).deny
    assert project[2].read_bytes() == original


def test_shared_worker_protocol_denies_exit_zero_deny(project):
    request = payload(project)
    request["han_hermes_worker"] = {"cwd": str(project[0]), "guard": [sys.executable, "-c",
        "print('{\"hookSpecificOutput\":{\"permissionDecision\":\"deny\"}}')"]}
    assert workers.project_denial(request)
    request["han_hermes_worker"]["guard"] = [sys.executable, "-c", "pass"]
    assert not workers.project_denial(request)


def test_policy_validation_and_marker_share_existing_active_lock(project, monkeypatch):
    configure(project, [sys.executable, "-c", "pass"])
    checked = threading.Event()
    release = threading.Event()
    contender_checked = threading.Event()
    outcomes = {}

    def validate(adapter, request):
        if request["session_id"] == "gardener-b":
            checked.set()
            assert release.wait(5)
            return ""
        contender_checked.set()
        return "owner changed" if json.loads(project[2].read_text())["session"] != "gardener-a" else ""

    monkeypatch.setattr(mission, "mission_policy_denial", validate)
    # FileLock uses timeout=0; contention must deny rather than overlap or deadlock.
    thread = threading.Thread(target=lambda: outcomes.setdefault("resume", execute_marker(payload(project))))
    thread.start()
    try:
        assert checked.wait(5)
        outcome = execute_marker(payload(project, session="gardener-a", action="close"))
        assert outcome.deny
        assert not contender_checked.is_set()
        assert json.loads(project[2].read_text())["session"] == "gardener-a"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not outcomes["resume"].deny
    assert json.loads(project[2].read_text())["session"] == "gardener-b"


def test_adapter_change_during_lock_acquisition_is_rejected(project, monkeypatch):
    configure(project, [sys.executable, "-c", "pass"])
    original = project[2].read_bytes()
    real_lock = mission_entry.active_lock

    def changed(state):
        data = json.loads(project[1].read_text())
        del data["guard"]
        project[1].write_text(json.dumps(data), encoding="utf-8")
        return real_lock(state)

    monkeypatch.setattr(mission_entry, "active_lock", changed)
    assert execute_marker(payload(project)).deny
    assert project[2].read_bytes() == original
