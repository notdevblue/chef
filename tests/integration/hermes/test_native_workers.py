"""Standalone leaf capabilities exercised with invented workspaces and host events."""

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.guards import adapters, mission
from chef.runtimes.hermes import native, plugin, research, workers


pytestmark = pytest.mark.skipif(os.name != "posix", reason="native Hermes requires POSIX file/process capabilities")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "comet"
    root.mkdir()
    adapter = root / ".hermes/orchestrator/adapter.json"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"},
        "guard": {"deny": [{"pattern": "forbidden-command", "reason": "invented policy"}]}}))
    state = root / "state"
    (state / "seed").mkdir(parents=True)
    (state / "ACTIVE").write_text(json.dumps({"id": "seed", "session": "parent", "runtime": "hermes"}))
    (state / "seed/mission.md").write_text("# Invented task\n")
    for name in ("HAN_ADAPTERS_DIR", "HAN_REPO", "CLAUDE_PROJECT_DIR", workers.WORKER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(adapters, "in_place_adapters_dir", lambda: str(tmp_path / "absent"))
    monkeypatch.setattr(research, "profile_config", lambda: {"delegation": {"inherit_mcp_toolsets": False}})
    return SimpleNamespace(root=root, state=state, active=mission.read_active(str(state)))


@pytest.fixture
def host(workspace, monkeypatch):
    class Lifecycle:
        def launch(self, request):
            self.request = request
            bridge.native.started(parent_session_id="foreign", child_session_id="intruder",
                child_subagent_id="wrong", child_goal=request["goal"])
            bridge.native.started(parent_session_id="parent", child_session_id="child-session",
                child_subagent_id="child", child_goal=request["goal"])
            return {"subagent_id": "child", "role": "leaf", "model": "invented-model", "provider": "invented-provider"}

        def wait(self, handle, timeout_seconds):
            self.observe()
            return {"completed": self.complete}

        def result(self, handle):
            return {"ready": True, "terminal_state": "SUCCEEDED", "summary": "observed"}

        def cancel(self, handle, reason):
            self.cancelled = True
            assert "error" in call(native.READ, {"operation": "list"})

    lifecycle = Lifecycle()
    lifecycle.observe = lambda: None
    lifecycle.complete, lifecycle.cancelled = True, False
    bridge = plugin.Bridge(lifecycle)
    monkeypatch.setattr(plugin, "native_cwd", lambda _: str(workspace.root))

    def call(tool, args, session="child-session"):
        return json.loads(bridge.native.tool(tool, args, session_id=session, task_id="child-task"))

    def run(role="dev-small", config=None):
        return bridge.native.run(role, "Implement the invented brief.", 10, str(workspace.root),
            str(workspace.state), workspace.active, None, None, config=config)

    return SimpleNamespace(bridge=bridge, lifecycle=lifecycle, call=call, run=run)


def test_developer_native_write_patch_and_test_flow(workspace, host):
    def observe():
        assert host.call(native.WRITE, {"operation": "write", "path": "sample.py", "content": "value = 1\n"})["bytes"] == 10
        assert host.call(native.WRITE, {"operation": "patch", "path": "sample.py", "old_text": "= 1", "new_text": "= 2"})["bytes"] == 10
        command = shlex.quote(sys.executable) + ' -c "from sample import value; assert value == 2; print(value)"'
        result = host.call(native.SHELL, {"command": command})
        assert result["exit_code"] == 0 and result["stdout"].strip() == "2", result
        assert host.call(native.READ, {"operation": "read", "path": "sample.py"})["text"] == "value = 2\n"
        assert host.bridge.before_tool("delegate_task", {}, "child-session", "child-task")["action"] == "block"
        assert host.bridge.before_tool("terminal", {}, "child-session", "child-task")["action"] == "block"
        assert "error" in host.call(native.READ, {"operation": "list"}, session="intruder")
    host.lifecycle.observe = observe
    assert host.run()["ok"]
    request = host.lifecycle.request
    assert request["allowed_toolsets"] == [native.READ_TOOLSET, native.WRITE_TOOLSET, native.SHELL_TOOLSET]
    assert "working_directory" not in request and "model" not in request and "provider" not in request
    assert host.bridge.before_tool("terminal", {}, "child-session", "child-task")["action"] == "block"
    assert "error" in host.call(native.READ, {"operation": "list"})


@pytest.mark.parametrize("role", workers.ROLES)
def test_role_tools_and_explicit_routing(workspace, host, role):
    def observe():
        direct = host.call(native.WRITE, {"operation": "write", "path": "sample.txt", "content": "text"})
        shell = host.call(native.SHELL, {"command": "printf evidence"})
        assert ("error" not in direct) == (role in native.DEVELOPERS)
        assert ("error" not in shell) == (role not in workers.READ_ONLY)
    host.lifecycle.observe = observe
    assert host.run(role, {"model": "chosen", "provider": "chosen-provider"})["ok"]
    assert host.lifecycle.request["model"] == "chosen"
    assert host.lifecycle.request["provider"] == "chosen-provider"
    assert host.lifecycle.request["role"] == "leaf"
    assert "Codex" not in host.lifecycle.request["context"]


@pytest.mark.parametrize("config", [False, [], "", 0])
def test_invalid_falsey_routing_is_rejected_before_assignment_or_launch(host, monkeypatch, config):
    assignments = []
    def assignment(*args, **kwargs):
        assignments.append((args, kwargs))
        raise AssertionError("invalid routing must not create an assignment")
    monkeypatch.setattr(host.bridge.native, "assignment", assignment)
    with pytest.raises(ValueError, match="native role routing"):
        host.run(config=config)
    assert not assignments and not hasattr(host.lifecycle, "request")


def test_paged_read_and_path_denials(workspace, host, tmp_path):
    (workspace.root / "long.txt").write_text("a" * research.READ_LIMIT + "tail")
    outside = tmp_path / "outside.txt"
    outside.write_text("private invented text")
    (workspace.root / "alias").symlink_to(outside)
    (workspace.root / "alias-dir").symlink_to(tmp_path, target_is_directory=True)
    instructions = workspace.root / ".codex/agents/reviewer.toml"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("Review the invented sample read-only.")
    def observe():
        first = host.call(native.READ, {"operation": "read", "path": "long.txt"})
        assert first["truncated"] and first["next_offset"] == research.READ_LIMIT
        assert host.call(native.READ, {"operation": "read", "path": "long.txt", "offset": first["next_offset"]})["text"] == "tail"
        assert host.call(native.READ, {"operation": "read", "path": ".codex/agents/reviewer.toml"})["text"] == instructions.read_text()
        assert "error" in host.call(native.WRITE, {"operation": "write", "path": ".codex/agents/reviewer.toml", "content": "wrong"})
        for path in ("../outside.txt", str(outside), "alias", "alias-dir/outside.txt", "state/ACTIVE", ".hermes/orchestrator/adapter.json", ".git/config"):
            assert "error" in host.call(native.READ, {"operation": "read", "path": path})
            assert "error" in host.call(native.WRITE, {"operation": "write", "path": path, "content": "wrong"})
        assert "error" in host.call(native.READ, {"operation": "read", "path": "long.txt", "session_id": "parent"})
        assert "error" in host.call(native.READ, {"operation": "list", "offset": 1})
    host.lifecycle.observe = observe
    host.run()
    assert outside.read_text() == "private invented text"


def test_readonly_and_concurrently_modified_files_are_preserved(workspace, monkeypatch):
    target = workspace.root / "sample.txt"
    target.write_text("original")
    manager = native.NativeWorkers(None)
    with manager.assignment("dev-small", workspace.root, workspace.state, workspace.active, None, None) as job:
        target.chmod(0o444)
        with pytest.raises(ValueError, match="read-only"):
            job.call(native.WRITE, {"operation": "write", "path": "sample.txt", "content": "replacement"}, "task")
        assert target.read_text() == "original"
        target.chmod(0o644)
        original = job.file_version
        calls = 0
        def version(directory, name):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text("concurrent")
            return original(directory, name)
        monkeypatch.setattr(job, "file_version", version)
        with pytest.raises(ValueError, match="destination changed"):
            job.call(native.WRITE, {"operation": "patch", "path": "sample.txt", "old_text": "original", "new_text": "replacement"}, "task")
        assert target.read_text() == "concurrent"
        assert not list(workspace.root.glob(".chef-write-*"))


def test_guard_command_receives_child_only_schema1_context_for_each_tool(workspace, tmp_path):
    guard = tmp_path / "guard.py"
    log = tmp_path / "calls.jsonl"
    guard.write_text("import json, os, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "context = json.loads(pathlib.Path(os.environ['CHEF_HERMES_WORKER']).read_text())\n"
        "assert context['schema'] == 1 and context['pid'] > 0\n"
        "assert context['role'] == 'dev-small' and context['session'] == payload['session_id'] == 'parent'\n"
        "assert context['cwd'] == payload['cwd'] and context['backend'] == 'hermes'\n"
        "with pathlib.Path(sys.argv[1]).open('a') as stream: stream.write(payload['tool_name'] + '\\n')\n")
    original = os.environ.get(workers.WORKER_ENV)
    with native.NativeWorkers(None).assignment("dev-small", workspace.root, workspace.state, workspace.active,
            [sys.executable, str(guard), str(log)], None) as job:
        context = job.context_path
        assert job.call(native.READ, {"operation": "list"}, "task")["entries"] == []
        job.call(native.WRITE, {"operation": "write", "path": "sample.txt", "content": "text"}, "task")
        job.call(native.SHELL, {"command": "printf checked"}, "task")
    assert log.read_text().splitlines() == [native.READ, "Write", "Bash"]
    assert not context.exists() and os.environ.get(workers.WORKER_ENV) == original


def test_write_and_patch_guards_receive_source_and_deny_before_mutation(workspace, tmp_path, monkeypatch):
    guard = tmp_path / "content_guard.py"
    log = tmp_path / "content_payloads.jsonl"
    guard.write_text("import json, pathlib, sys\n"
        "payload = json.load(sys.stdin)\n"
        "with pathlib.Path(sys.argv[1]).open('a') as stream: stream.write(json.dumps(payload) + '\\n')\n"
        "inputs = payload['tool_input']\n"
        "if 'blocked-text' in inputs.get('content', '') or 'blocked-text' in inputs.get('new_string', ''):\n"
        "    print(json.dumps({'decision': 'block', 'reason': 'content policy rejected source'}))\n")
    shared = []
    decide = mission.decide
    def observe(payload):
        shared.append(payload)
        return decide(payload)
    monkeypatch.setattr(mission, "decide", observe)
    target = workspace.root / "sample.txt"
    target.write_text("original text")
    with native.NativeWorkers(None).assignment("dev-small", workspace.root, workspace.state, workspace.active,
            [sys.executable, str(guard), str(log)], None) as job:
        for path in ("new.txt", "sample.txt"):
            with pytest.raises(ValueError, match="content policy rejected source"):
                job.call(native.WRITE, {"operation": "write", "path": path, "content": "blocked-text"}, "task")
        with pytest.raises(ValueError, match="content policy rejected source"):
            job.call(native.WRITE, {"operation": "patch", "path": "sample.txt",
                "old_text": "original", "new_text": "blocked-text"}, "task")
        assert not (workspace.root / "new.txt").exists() and target.read_text() == "original text"
        job.call(native.WRITE, {"operation": "patch", "path": "sample.txt",
            "old_text": "original", "new_text": "reviewed"}, "task")
    project = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(project) == len(shared) == 4
    for project_payload, shared_payload in zip(project, shared):
        assert project_payload["tool_input"] == shared_payload["tool_input"]
    assert project[0]["tool_name"] == "Write"
    assert project[0]["tool_input"] == {"file_path": str(workspace.root / "new.txt"), "content": "blocked-text"}
    assert project[2]["tool_name"] == "Edit"
    assert project[2]["tool_input"] == {"file_path": str(target), "old_string": "original", "new_string": "blocked-text"}
    assert project[3]["tool_input"] == {"file_path": str(target), "old_string": "original", "new_string": "reviewed"}
    assert target.read_text() == "reviewed text" and not list(workspace.root.glob(".chef-write-*"))


def test_shared_policy_and_protected_state_denials(workspace):
    manager = native.NativeWorkers(None)
    with manager.assignment("dev-small", workspace.root, workspace.state, workspace.active, None, None) as job:
        with pytest.raises(ValueError, match="invented policy"):
            job.call(native.SHELL, {"command": "forbidden-command"}, "task")
        with pytest.raises(ValueError):
            job.call(native.SHELL, {"command": "printf changed > state/ACTIVE"}, "task")
        with pytest.raises(ValueError):
            job.call(native.SHELL, {"command": "echo han-mission close seed"}, "task")
        # The command name is opaque to literal-target extraction. Its mutation
        # must still invalidate the assignment without rolling back the file.
        helper = workspace.root / "helper.py"
        helper.write_text("from pathlib import Path\nPath('state/seed/mission.md').write_text('changed')\n")
        with pytest.raises(ValueError, match="protected mission state changed"):
            job.call(native.SHELL, {"command": shlex.quote(sys.executable) + " helper.py"}, "task")
        with pytest.raises(ValueError, match="no longer active"):
            job.call(native.READ, {"operation": "list"}, "task")
    assert (workspace.state / "seed/mission.md").read_text() == "changed"


def test_timeout_revokes_before_cancel_and_foreign_owner_blocks_all_tools(workspace, host):
    host.lifecycle.complete = False
    with pytest.raises(ValueError, match="timed out"):
        host.run()
    assert host.lifecycle.cancelled
    assert host.bridge.before_tool("delegate_task", {}, "child-session", "task")["action"] == "block"
    with native.NativeWorkers(None).assignment("dev-small", workspace.root, workspace.state, workspace.active, None, None) as job:
        (workspace.state / "ACTIVE").write_text(json.dumps({"id": "seed", "session": "foreign", "runtime": "hermes"}))
        for tool, args in ((native.READ, {"operation": "list"}),
                (native.WRITE, {"operation": "write", "path": "sample.txt", "content": "wrong"}),
                (native.SHELL, {"command": "printf wrong"})):
            with pytest.raises(ValueError, match="no longer active"):
                job.call(tool, args, "task")
    assert not (workspace.root / "sample.txt").exists()


def test_shell_timeout_output_limit_and_cancel_drain_process_group(workspace):
    manager = native.NativeWorkers(None)
    with manager.assignment("verifier", workspace.root, workspace.state, workspace.active, None, None) as job:
        with pytest.raises(ValueError, match="timed out"):
            job.call(native.SHELL, {"command": "sleep 30", "timeout": 1}, "task")
        command = shlex.quote(sys.executable) + ' -c "print(\'x\' * 200000)"'
        # Fast exits are truncated; a running noisy process is stopped.
        try:
            result = job.call(native.SHELL, {"command": command}, "task")
            assert result["truncated"] and len(result["stdout"]) == native.OUTPUT_LIMIT
        except ValueError as exc:
            assert "output limit" in str(exc)
        errors = []
        def command_call():
            try:
                job.call(native.SHELL, {"command": "sleep 30"}, "task")
            except ValueError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=command_call)
        thread.start()
        deadline = time.monotonic() + 5
        while job.process is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert job.process is not None
        pid = job.process.pid
        job.revoke()
        job.drain()
        thread.join(timeout=5)
        assert not thread.is_alive() and errors
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
