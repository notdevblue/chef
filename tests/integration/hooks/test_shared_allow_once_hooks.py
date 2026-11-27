"""The source hook and a second generic participant share one native call."""

import json

import pytest

from chef.guards import allow_once as broker
from chef.guards import mission
from tests.integration.hooks.test_mission_guard import (
    NO_REGISTRY, PLUGIN_ROOT, SCRIPT, STATE, denial, make_active, make_adapter,
    project, run_raw, write_allow_once,
)


def shared_fixture(project, monkeypatch, runtime, command):
    make_adapter(project)
    make_active(project, "m-demo")
    adapter_path = project / ".claude/orchestrator/adapter.json"
    data = json.loads(adapter_path.read_text(encoding="utf-8"))
    data["guard"]["allowOnceParticipants"] = ["chef-mission", "alpha"]
    adapter_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(NO_REGISTRY))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    payload = {
        "han_runtime": runtime, "session_id": "s1", "tool_use_id": "fixture-call",
        "cwd": str(project), "tool_name": "Bash",
        "tool_input": {"command": command, "shell": "pwsh" if runtime == "codex" else "bash"},
    }
    located = mission.find_adapter(payload)
    adapter = mission.load_adapter(located.adapter_path, runtime=runtime)
    state = project / STATE
    write_allow_once(project, "m-demo", [command])
    return payload, adapter, state


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("command", ["p4 submit -c 17", "widget inspect"])
@pytest.mark.parametrize("chef_first", [True, False])
def test_source_hook_shared_with_generic_participant(project, monkeypatch, runtime, command, chef_first):
    payload, adapter, state = shared_fixture(project, monkeypatch, runtime, command)
    script = PLUGIN_ROOT / "codex/mission_guard.py" if runtime == "codex" else SCRIPT

    def chef():
        result = run_raw(json.dumps(payload).encode(), env_project=project, script=script)
        assert result.returncode == 0, result.stderr.decode()
        return json.loads(result.stdout or b"{}")

    def alpha():
        return broker.consume_shared_allow_once(
            state, "m-demo", command, payload=payload, participant="alpha",
            participants=adapter["guard"]["allowOnceParticipants"], active=mission.read_active(state),
            config=broker.shared_once_context(adapter),
        )

    if chef_first:
        assert not denial(chef())
        assert alpha()
    else:
        assert alpha()
        assert not denial(chef())
    assert "already consumed" in denial(chef())
    with pytest.raises(broker.SharedAllowOnceError, match="already consumed"):
        alpha()
    assert (state / "m-demo/allow-once").read_bytes() == b""


@pytest.mark.parametrize("protection", ["execution", "question", "worker"])
def test_hard_protections_run_before_shared_consumption(project, monkeypatch, protection):
    command = "p4 submit -c 17"
    payload, _adapter, state = shared_fixture(project, monkeypatch, "claude", command)
    if protection == "execution":
        monkeypatch.setattr(mission, "execution_write_reason", lambda _payload: "fixture execution protection")
    elif protection == "question":
        monkeypatch.setattr(mission.question_write_guard, "question_write_reason", lambda *_args: "fixture question protection")
    else:
        from chef.workers import context

        payload["han_worker"] = True
        monkeypatch.setattr(context, "denial", lambda _payload: "fixture worker protection")
    before = (state / "m-demo/allow-once").read_bytes()
    assert "fixture" in mission.decide(payload).deny
    assert (state / "m-demo/allow-once").read_bytes() == before
    assert not (state / "m-demo/allow-once.receipts").exists()


def test_normal_command_without_opt_in_keeps_legacy_token(project, monkeypatch):
    make_adapter(project)
    make_active(project, "m-demo")
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(NO_REGISTRY))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    token = write_allow_once(project, "m-demo", ["widget inspect"])
    payload = {"tool_name": "Bash", "tool_input": {"command": "widget inspect"}, "cwd": str(project), "session_id": "s1"}
    assert not mission.decide(payload).deny
    assert token.read_text(encoding="utf-8") == "widget inspect\n"
