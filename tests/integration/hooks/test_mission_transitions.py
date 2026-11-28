import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from chef.guards import mission
from chef.mission import transitions
from tests.paths import PLUGIN_ROOT


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "fictional-workspace"
    folder = root / ".claude/orchestrator"
    folder.mkdir(parents=True)
    (folder / "adapter.json").write_text(json.dumps({
        "schema": 1, "paths": {"stateDir": "state"}}), encoding="utf-8")
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "no-registry"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHEF_WORKER_CONTEXT", raising=False)
    monkeypatch.delenv("CHEF_HERMES_WORKER", raising=False)
    monkeypatch.chdir(root)
    return root


def command(root, action="open", runtime="claude", session="parent"):
    return [sys.executable, "-B", str(PLUGIN_ROOT / "mission_control.py"), action, "sample-mission",
            "--runtime", runtime, "--session", session, "--project", str(root), "--request", uuid.uuid4().hex]


def preflight(root, argv, runtime="claude", **extra):
    payload = {"cwd": str(root), "session_id": "parent", "tool_name": "Bash",
               "tool_input": {"command": " ".join('"' + item + '"' if ' ' in item else item for item in argv)},
               **extra}
    if runtime == "grok":
        payload["hookEventName"] = "pre_tool_use"
        payload["toolName"] = "run_terminal_command"
    hook = PLUGIN_ROOT / ("hooks/mission_guard.py" if runtime == "claude" else
                          "codex/mission_guard.py" if runtime == "codex" else "hooks/mission_guard.py")
    result = subprocess.run([sys.executable, "-B", str(hook)], input=json.dumps(payload).encode(),
                            capture_output=True, cwd=root, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    return json.loads(result.stdout or b"{}")


def denied(result):
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def commit(argv):
    return subprocess.run(argv, capture_output=True, timeout=20)


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("unified", [False, True])
def test_permission_check_does_not_open_until_cli_executes(workspace, runtime, unified):
    argv = command(workspace, runtime=runtime)
    if unified:
        argv[2:3] = [str(PLUGIN_ROOT / "chef_cli.py"), "mission"]
    assert not denied(preflight(workspace, argv, runtime))
    assert not (workspace / "state/ACTIVE").exists()
    assert not (workspace / "state/sample-mission/sessions.tsv").exists()
    result = commit(argv)
    assert result.returncode == 0, result.stderr.decode()
    active = mission.read_active(str(workspace / "state"))
    assert active.runtime == runtime and active.session == "parent"
    assert commit(argv).returncode == 1


def test_unified_transition_rejects_a_different_bundle(workspace):
    argv = command(workspace)
    argv[2:3] = [str(workspace / "chef_cli.py"), "mission"]
    assert denied(preflight(workspace, argv))
    assert not (workspace / "state/.transitions").exists()


@pytest.mark.parametrize("previous", ["claude", "codex", "grok", ""])
@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_resume_updates_both_identity_fields_and_preserves_open_time(workspace, previous, runtime):
    state = str(workspace / "state")
    mission.write_active(state, mission.Active("sample-mission", "old-session", "original-time", runtime=previous))
    argv = command(workspace, "resume", runtime)
    assert not denied(preflight(workspace, argv, runtime))
    assert mission.read_active(state).session == "old-session"
    result = commit(argv)
    assert result.returncode == 0, result.stderr.decode()
    active = mission.read_active(state)
    assert (active.session, active.runtime, active.opened_at) == ("parent", runtime, "original-time")


def test_host_denial_after_preflight_preserves_active_and_allowance(workspace):
    state = workspace / "state"
    mission.write_active(str(state), mission.Active("sample-mission", "parent", "original", runtime="claude"))
    allowance = state / "sample-mission/allow-once"
    allowance.parent.mkdir(parents=True)
    allowance.write_bytes(b"fictional-command\n")
    original = (state / "ACTIVE").read_bytes()
    argv = command(workspace, "close")
    assert not denied(preflight(workspace, argv))
    # The host denies execution: it never starts the prepared CLI.
    assert (state / "ACTIVE").read_bytes() == original
    assert allowance.read_bytes() == b"fictional-command\n"
    assert commit(argv).returncode == 0
    assert not (state / "ACTIVE").exists() and not allowance.exists()


@pytest.mark.parametrize("change", ["owner", "adapter", "expiry"])
def test_changed_inputs_refuse_execution_without_mutation(workspace, change):
    argv = command(workspace)
    assert not denied(preflight(workspace, argv))
    if change == "owner":
        mission.write_active(str(workspace / "state"), mission.Active("other-mission", "other-session", "time"))
    elif change == "adapter":
        (workspace / ".claude/orchestrator/adapter.json").write_text(
            '{"schema":1,"project":"changed","paths":{"stateDir":"state"}}', encoding="utf-8")
    else:
        path = next((workspace / "state/.transitions").glob("*.json"))
        data = json.loads(path.read_text(encoding="utf-8")); data["createdAt"] -= 1000
        path.write_text(json.dumps(data), encoding="utf-8")
    before = mission.read_active(str(workspace / "state"))
    assert commit(argv).returncode == 1
    assert mission.read_active(str(workspace / "state")) == before


@pytest.mark.parametrize("extra", [{"session_id": "other"}, {"agent_id": "child"}])
def test_native_identity_must_match_and_workers_cannot_prepare(workspace, extra):
    assert denied(preflight(workspace, command(workspace), **extra))
    assert not (workspace / "state/ACTIVE").exists()


@pytest.mark.parametrize("action", ["open", "resume", "close"])
def test_cli_without_hook_receipt_and_legacy_echo_cannot_change_state(workspace, action):
    assert commit(command(workspace)).returncode == 1
    if action != "open":
        mission.write_active(str(workspace / "state"), mission.Active("sample-mission", "parent", "time", runtime="claude"))
    before = mission.read_active(str(workspace / "state"))
    payload = {"cwd": str(workspace), "session_id": "parent", "han_runtime": "claude",
               "tool_name": "Bash", "tool_input": {"command": f"echo han-mission {action} sample-mission"}}
    assert mission.decide(payload).deny
    assert mission.read_active(str(workspace / "state")) == before


def test_policy_is_rechecked_when_prepared_command_executes(workspace):
    gate = workspace / "gate.txt"
    gate.write_text("deny", encoding="utf-8")
    policy = workspace / "policy.py"
    policy.write_text("import pathlib,sys\nsys.exit(0 if pathlib.Path(sys.argv[1]).read_text() == 'allow' else 1)\n",
                      encoding="utf-8")
    adapter = workspace / ".claude/orchestrator/adapter.json"
    value = json.loads(adapter.read_text(encoding="utf-8"))
    value["guard"] = {"missionPolicyCommand": [sys.executable, str(policy), str(gate)]}
    adapter.write_text(json.dumps(value), encoding="utf-8")
    assert denied(preflight(workspace, command(workspace)))
    gate.write_text("allow", encoding="utf-8")
    argv = command(workspace)
    assert not denied(preflight(workspace, argv))
    gate.write_text("deny", encoding="utf-8")
    assert commit(argv).returncode == 1
    assert not (workspace / "state/ACTIVE").exists()


def test_session_log_failure_restores_previous_active(workspace, monkeypatch):
    from chef.infrastructure import storage

    state = str(workspace / "state")
    mission.write_active(state, mission.Active("sample-mission", "old-session", "original", runtime="codex"))
    before = (workspace / "state/ACTIVE").read_bytes()
    argv = command(workspace, "resume")
    assert not denied(preflight(workspace, argv))
    original = storage.atomic_write_bytes

    def fail_log(path, value, **kwargs):
        if Path(path).name == "sessions.tsv":
            raise PermissionError("fictional log error")
        return original(path, value, **kwargs)

    monkeypatch.setattr(storage, "atomic_write_bytes", fail_log)
    with pytest.raises(ValueError, match="ACTIVE"):
        transitions.execute(transitions.arguments(argv[3:]))
    assert (workspace / "state/ACTIVE").read_bytes() == before
    assert not (workspace / "state/sample-mission/sessions.tsv").exists()


def test_two_prepared_calls_cannot_overwrite_a_committed_owner(workspace):
    first = command(workspace)
    second = command(workspace, session="next-session")
    assert not denied(preflight(workspace, first))
    assert not denied(preflight(workspace, second, session_id="next-session"))
    assert commit(first).returncode == 0
    before = (workspace / "state/ACTIVE").read_bytes()
    assert commit(second).returncode == 1
    assert (workspace / "state/ACTIVE").read_bytes() == before


def test_resume_preserves_existing_session_history_bytes(workspace):
    state = str(workspace / "state")
    mission.write_active(state, mission.Active("sample-mission", "old-session", "original", runtime="codex"))
    history = workspace / "state/sample-mission/sessions.tsv"
    history.parent.mkdir(parents=True)
    previous = b"at\taction\tsession\ttranscript\r\nold\topen\told-session\t\r\n"
    history.write_bytes(previous)
    argv = command(workspace, "resume")
    assert not denied(preflight(workspace, argv))
    assert commit(argv).returncode == 0
    assert history.read_bytes().startswith(previous)


@pytest.mark.parametrize("prefix", [["env", "SAMPLE=1"], ["cmd", "/c"]])
def test_wrapped_transition_cannot_skip_native_identity_check(workspace, prefix):
    assert denied(preflight(workspace, prefix + command(workspace), agent_id="child"))
    assert not (workspace / "state/ACTIVE").exists()


def test_searching_transition_source_is_still_read_only(workspace):
    payload = {"cwd": str(workspace), "session_id": "parent", "han_runtime": "claude",
               "tool_name": "Bash", "tool_input": {"command": 'rg "mission_control.py" README.md'}}
    assert not mission.decide(payload).deny


def test_cli_without_adapter_returns_an_error_without_traceback(workspace):
    (workspace / ".claude/orchestrator/adapter.json").unlink()
    result = commit(command(workspace))
    assert result.returncode == 1
    assert "error" in json.loads(result.stderr)
