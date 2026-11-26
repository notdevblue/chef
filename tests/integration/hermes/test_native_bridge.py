"""Native Hermes policy boundary with fictional local workspaces."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.runtimes.hermes import plugin


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = tmp_path / "comet"
    registration = project / ".hermes" / "orchestrator"
    registration.mkdir(parents=True)
    adapter = {
        "schema": 1, "project": "Comet", "paths": {"vcsRoot": ".", "stateDir": "state"},
        "guard": {"coordinatorWrite": ["state/**"], "deny": [{"pattern": "dangerous-operation", "reason": "fictional operation denied"}]},
        "agents": {"allow": ["chef:researcher"]},
    }
    (registration / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    state = project / "state"
    state.mkdir()
    (state / "ACTIVE").write_text(json.dumps({"id": "mission", "session": "owner", "runtime": "hermes"}), encoding="utf-8")
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty-registry"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr(plugin, "native_cwd", lambda _: str(project))
    bridge = plugin.Bridge()
    return SimpleNamespace(project=project, bridge=bridge, adapter=adapter, registration=registration)


def call(world, tool, args, session="owner", **kwargs):
    return world.bridge.before_tool(tool, args, session_id=session, task_id="task", **kwargs)


def test_bootstrap_context_exposes_actual_python_even_before_adapter_selection(world, monkeypatch):
    for located in (None, SimpleNamespace(project=str(world.project))):
        monkeypatch.setattr(plugin.mission, "locate_adapter", lambda *_: located)
        context = world.bridge.context(session_id="owner", task_id="task")["context"]
        assert json.dumps(sys.executable) in context
        assert "absolute interpreter path" in context
    def unavailable(*_):
        raise ValueError("fictional registry unavailable")
    monkeypatch.setattr(plugin.mission, "locate_adapter", unavailable)
    assert json.dumps(sys.executable) in world.bridge.context(session_id="owner", task_id="task")["context"]


def test_native_terminal_denial_prevents_execution(world):
    assert call(world, "terminal", {"command": "dangerous-operation"})["action"] == "block"
    assert call(world, "terminal", {"command": "echo safe"}) is None


def test_worker_status_forwards_the_trusted_task_workspace(world, monkeypatch, tmp_path):
    from chef.runtimes.hermes import workers

    calls = []
    def status(adapter, research, native=None, cwd=None):
        calls.append((adapter, research, native, cwd))
        return {"available": True}
    monkeypatch.setattr(workers, "status", status)
    monkeypatch.setattr(plugin, "native_cwd", lambda _: str(tmp_path / "unrelated"))
    world.bridge.bindings[("owner", "task")] = {"cwd": str(world.project)}
    result = json.loads(world.bridge.worker_status({}, session_id="owner", task_id="task", cwd="untrusted"))
    assert result == {"available": True}
    assert len(calls) == 1 and calls[0][3] == str(world.project)
    assert calls[0][0]["project"] == "Comet"
    assert calls[0][1] is world.bridge.research and calls[0][2] is world.bridge.native


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "echo han-mission resume mission"}),
    ("write_file", {"path": "state/note.md", "content": "x"}),
    ("patch", {"mode": "replace", "path": "state/note.md", "old_string": "x", "new_string": "y"}),
])
def test_another_session_cannot_take_active_workspace(world, tool, args):
    before = (world.project / "state/ACTIVE").read_bytes()
    assert call(world, tool, args, session="stranger")["action"] == "block"
    assert (world.project / "state/ACTIVE").read_bytes() == before


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "source.py", "content": "x"}),
    ("patch", {"mode": "replace", "path": "source.py", "old_string": "x", "new_string": "y"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: source.py\n+x\n*** End Patch"}),
])
def test_coordinator_write_policy_applies_to_each_native_write_form(world, tool, args):
    assert call(world, tool, args)["action"] == "block"


def test_protected_question_cannot_be_patched(world):
    result = call(world, "patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: state/mission/ask-1.md\n+x\n*** End Patch"})
    assert result and result["action"] == "block"


def test_native_delegation_and_code_execution_are_not_role_worker_bypasses(world):
    for tool in ("delegate_task", "execute_code"):
        assert call(world, tool, {"goal": "pretend review"})["action"] == "block"
    assert call(world, "chef_delegate", {"role": "chef:researcher", "task": "inspect"}) is None
    assert call(world, "chef_delegate", {"role": "unlisted", "task": "inspect"})["action"] == "block"


def test_model_supplied_identity_never_weakens_policy(world):
    assert call(world, "write_file", {"path": "source.py", "content": "x", "agent_id": "fake", "session_id": "fake"})["action"] == "block"


def test_ignored_native_context_keys_cannot_redirect_guard(world, tmp_path):
    assert call(world, "terminal", {"command": "dangerous-operation", "cwd": str(tmp_path), "shell": "powershell"})["action"] == "block"
    payload = plugin.canonical_payload("terminal", {"command": "echo safe", "cwd": "/unrelated", "shell": "pwsh"}, "owner", "task", str(world.project))
    assert "cwd" not in payload["tool_input"] and "shell" not in payload["tool_input"]


def test_guard_failure_and_missing_trusted_identity_fail_closed(world, monkeypatch):
    assert world.bridge.before_tool("terminal", {"command": "echo safe"})["action"] == "block"
    def broken(*_):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(plugin.mission, "decide", broken)
    assert call(world, "terminal", {"command": "echo safe"})["action"] == "block"


@pytest.mark.parametrize("guard", [{"deny": ["("]}, {"coordinatorWrite": "invalid"}, ["invalid"]])
def test_invalid_configured_policy_never_silently_allows_tools(world, guard):
    world.adapter["guard"] = guard
    (world.registration / "adapter.json").write_text(json.dumps(world.adapter), encoding="utf-8")
    assert call(world, "terminal", {"command": "echo safe"})["action"] == "block"


def test_restart_with_no_memory_bindings_still_checks_owned_question_integrity(world, monkeypatch):
    checked = []
    def check(payload, runtime):
        checked.append(payload)
        return {"decision": "block", "reason": "fictional missing question receipt"}
    monkeypatch.setattr(plugin.integrity, "decide", check)
    assert not world.bridge.bindings
    outcome = world.bridge.verify(session_id="owner", task_id="task")
    assert outcome == {"action": "continue", "message": "fictional missing question receipt"}
    assert checked[0]["cwd"] == str(world.project)


def test_request_failure_is_carried_to_pretool_instead_of_raising(world, monkeypatch):
    def broken(*_):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(plugin, "native_cwd", broken)
    result = world.bridge.request("terminal", {"command": "echo safe"}, session_id="owner", task_id="task")
    assert call(world, "terminal", result["args"])["action"] == "block"


def test_bound_tasks_get_independent_terminal_and_file_paths(world):
    other = world.project.parent / "other-slot"
    for sid, cwd in (("owner", world.project), ("second", other)):
        world.bridge.bindings[(sid, sid)] = {"cwd": str(cwd)}
        terminal = world.bridge.request("terminal", {"command": "pwd"}, session_id=sid, task_id=sid)
        assert terminal["args"]["workdir"] == str(cwd)
        file = world.bridge.request("write_file", {"path": "file.txt", "content": "x"}, session_id=sid, task_id=sid)
        assert file["args"]["path"] == str(cwd / "file.txt")
        patch = world.bridge.request("patch", {"mode": "patch", "patch": "*** Update File: file.txt\n@@\n-x\n+y"}, session_id=sid, task_id=sid)
        assert str(cwd / "file.txt") in patch["args"]["patch"]


def test_explicit_workdir_cannot_escape_original_ownership_check(world, tmp_path):
    world.bridge.bindings[("stranger", "task")] = {"cwd": str(world.project)}
    assert call(world, "terminal", {"command": "echo safe", "workdir": str(tmp_path)}, session="stranger")["action"] == "block"


def test_case_distinct_projects_are_not_deduplicated_in_patch_ownership(world, tmp_path):
    foreign = tmp_path / "Comet"
    foreign.mkdir(exist_ok=True)
    if os.path.samefile(foreign, world.project):
        pytest.skip("volume does not support case-distinct project directories")
    registration = foreign / ".hermes" / "orchestrator"
    registration.mkdir(parents=True)
    # Allow ordinary source writes so the ownership check is the only reason
    # for blocking this multi-project operation.
    body = {"schema": 1, "paths": {"stateDir": "state"}}
    (registration / "adapter.json").write_text(json.dumps(body), encoding="utf-8")
    (world.registration / "adapter.json").write_text(json.dumps(body), encoding="utf-8")
    (foreign / "state").mkdir()
    (foreign / "state" / "ACTIVE").write_text(json.dumps({
        "id": "foreign-mission", "session": "different-owner", "runtime": "hermes",
    }), encoding="utf-8")
    patch = (
        f"*** Begin Patch\n*** Add File: {foreign}/source.py\n+x\n"
        f"*** Add File: {world.project}/source.py\n+x\n*** End Patch"
    )
    result = call(world, "patch", {"mode": "patch", "patch": patch})
    assert result and result["action"] == "block"
    assert "another active mission session" in result["message"]


def test_dry_slot_selection_does_not_change_binding_or_native_context(world, monkeypatch):
    from chef.slots import commands
    calls = []
    context = {"cwd": str(world.project), "runtime": "hermes", "session_id": "owner"}
    monkeypatch.setattr(commands, "current_session_context", lambda *a, **kw: (calls.append(kw) or context))
    monkeypatch.setattr(plugin, "bind_native_cwd", lambda *a: pytest.fail("dry run must not bind"))
    result = json.loads(world.bridge.slot({"project": str(world.project), "name": "slot-a", "dry_run": True}, session_id="owner", task_id="task"))
    assert result == context and not world.bridge.bindings
    assert calls == [{"resume": False, "dry_run": True}]


def test_failed_binding_change_never_runs_preparation(world, monkeypatch):
    from chef.slots import commands
    calls = []
    monkeypatch.setattr(commands, "current_session_context", lambda *a, **kw: (calls.append(kw) or {"cwd": "/fictional/different"}))
    world.bridge.bindings[("owner", "task")] = {"cwd": str(world.project)}
    result = json.loads(world.bridge.slot({"project": str(world.project), "name": "slot-b"}, session_id="owner", task_id="task"))
    assert "error" in result and calls == [{"resume": False, "dry_run": True}]


def test_native_registration_wires_guards_middleware_tools_and_skills(monkeypatch):
    class Context:
        def __init__(self):
            self.hooks, self.middleware, self.tools, self.skills = {}, {}, {}, {}
        def register_hook(self, name, callback):
            self.hooks[name] = callback
        def register_middleware(self, name, callback):
            self.middleware[name] = callback
        def register_tool(self, name, toolset, schema, handler):
            self.tools[name] = (schema, handler)
        def register_skill(self, name, path, description):
            self.skills[name] = path
    monkeypatch.setattr(plugin.sys, "platform", "linux")
    ctx = Context()
    plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "pre_llm_call", "pre_verify"}
    assert set(ctx.middleware) == {"tool_request"}
    assert set(ctx.tools) == {"chef_slot", "chef_mission", "chef_delegate", "chef_worker_status"}
    assert set(ctx.skills) == set(plugin.SKILLS)
