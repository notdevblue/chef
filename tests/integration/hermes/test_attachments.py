"""Named bridge transport and shared worker admission with invented data."""

import contextlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from chef.guards import adapters, mission
from chef.infrastructure.locking import LockBusy
from chef.runtimes.hermes import attachment, workers


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "orchard"
    root.mkdir()
    selected = root / ".hermes/orchestrator/adapter.json"
    selected.parent.mkdir(parents=True)
    selected.write_text(json.dumps({"schema": 1, "project": "imaginary-orchard", "paths": {"stateDir": "state"},
        "agents": {"allow": ["chef:*"]},
        "guard": {"deny": [{"pattern": "forbidden-action", "reason": "fictional policy"}]}}), encoding="utf-8")
    state = root / "state"
    (state / "orchard-mission").mkdir(parents=True)
    (state / "orchard-mission/mission.md").write_text("# Imaginary mission\n", encoding="utf-8")
    (state / "ACTIVE").write_text(json.dumps({"id": "orchard-mission", "session": "orchard-owner",
                                               "runtime": "hermes", "openedAt": "2026-01-01"}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "gateway"))
    for name in ("HAN_ADAPTERS_DIR", "HAN_REPO", "CLAUDE_PROJECT_DIR", workers.WORKER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(adapters, "in_place_adapters_dir", lambda: str(tmp_path / "absent"))
    return SimpleNamespace(root=root, state=state, adapter=selected, active=mission.read_active(str(state)))


@pytest.fixture
def bridge(tmp_path):
    script = tmp_path / "model bridge.py"
    script.write_text('''import json, os, subprocess, sys, time
from pathlib import Path
mode = sys.argv[1]
def send(value):
    print(json.dumps({"schema": 1, **value}), flush=True)
def receive():
    return json.loads(sys.stdin.readline())
assert receive() == {"schema": 1, "action": "status"}
if mode == "bad-status":
    print("not-json", flush=True)
    sys.exit(0)
send({"type":"status", "transport":"chef-tools-v1", "roles":["chef:researcher", "chef:dev-small"]})
if mode == "no-read":
    time.sleep(10)
line = sys.stdin.readline()
if not line:
    sys.exit(0)
request = json.loads(line)
assert request["action"] == "delegate"
assert os.getcwd() != request["workspace"]
if mode in ("sleep", "sleep-child"):
    Path(sys.argv[2]).write_text(str(os.getpid()))
    if mode == "sleep-child":
        subprocess.Popen([sys.executable, "-c", "import os,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)", sys.argv[3]])
    while True:
        time.sleep(1)
if mode == "stderr":
    sys.stderr.write("x" * (2 * 1024 * 1024))
    sys.stderr.flush()
if mode == "large-line":
    print("x" * (2 * 1024 * 1024), flush=True)
if mode == "invalid":
    send({"type":"tool", "id":"one", "tool":"chef_worker_read", "arguments":{}, "session_id":"claimed-owner"})
elif mode in ("tools", "readonly", "command", "denied", "duplicate"):
    tool = "chef_worker_shell" if mode in ("command", "denied") else "chef_worker_write" if mode == "readonly" else "chef_worker_read"
    args = {"command": "forbidden-action" if mode == "denied" else "printf attached > note.txt", "timeout":2} if tool == "chef_worker_shell" else {"operation":"read", "path":"notice.txt"}
    send({"type":"tool", "id":"one", "tool":tool, "arguments":args})
    response = receive()
    if mode == "duplicate":
        send({"type":"tool", "id":"one", "tool":tool, "arguments":args})
    else:
        send({"type":"result", "ok":response["ok"], "result":json.dumps(response), "error":response.get("error", "")})
else:
    send({"type":"result", "ok":True, "result":"observed orchard", "error":""})
time.sleep(10)
''', encoding="utf-8")

    def configured(mode="tools", *args):
        return {"backend": "garden-bridge", "attachments": {"garden-bridge": {
            "command": [sys.executable, str(script), mode, *map(str, args)]}}}

    return configured


class FakeNative:
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.revoked = False

    def check_available(self):
        pass

    def check_tool_execution(self):
        pass

    @contextlib.contextmanager
    def assignment(self, role, cwd, state, active, checker, slot, config=None):
        self.role = role
        try:
            yield self
        finally:
            self.revoke()

    def check(self):
        if self.revoked or mission.read_active(str(self.project.state)) != self.project.active:
            raise ValueError("assignment ownership changed")

    def revoke(self):
        self.revoked = True

    def call(self, tool, args, task):
        self.check()
        self.calls.append((tool, args, task))
        if self.role == "researcher" and tool != "chef_worker_read":
            raise ValueError("this tool is unavailable to the assigned role")
        return {"text": "observed orchard"}


def configure(project, settings):
    adapter = json.loads(project.adapter.read_text(encoding="utf-8"))
    adapter["workers"] = settings
    project.adapter.write_text(json.dumps(adapter), encoding="utf-8")


def run(project, native, role="dev-small"):
    return workers.delegate({"role": role, "prompt": "Inspect the imaginary orchard.", "timeout": 10},
                            session_id="orchard-owner", cwd=str(project.root), native=native)


def test_default_hermes_never_probes_codex_and_passes_role_config(project, monkeypatch):
    native = FakeNative(project)
    configure(project, {"hermesRoles": {"dev-small": {"model": "imaginary-model"}}})
    seen = []
    native.run = lambda *args, **kwargs: (seen.append((args, kwargs)) or {"ok": True})
    monkeypatch.setattr(workers, "codex_executable", lambda: pytest.fail("unexpected Codex auth"))
    assert run(project, native) == {"ok": True}
    assert seen[0][0][0] == "dev-small"
    assert seen[0][1] == {"config": {"model": "imaginary-model"}}
    assert workers.status({}, native=native) == {
        "backend": "hermes", "roles": ["chef:" + role for role in workers.ROLES], "available": True}


def test_named_attachment_executes_literal_argv_and_mediates_host_identity(project, bridge):
    configure(project, bridge())
    native = FakeNative(project)
    result = run(project, native)
    assert result["ok"] and result["runtime"] == "garden-bridge"
    tool, args, task = native.calls[0]
    assert tool == "chef_worker_read" and args == {"operation": "read", "path": "notice.txt"}
    assert task.startswith("chef-attachment-") and task != "one"
    assert native.revoked


def test_attachment_status_intersects_advertised_roles_and_project_allow(project, bridge):
    native = FakeNative(project)
    adapter = {"workers": bridge(), "agents": {"allow": ["chef:dev-*"]}}
    assert workers.status(adapter, native=native) == {
        "backend": "garden-bridge", "roles": ["chef:dev-small"], "available": True}
    adapter["agents"]["allow"] = ["chef:verifier"]
    assert "no roles" in workers.status(adapter, native=native)["error"]


def test_attachment_status_needs_no_hermes_lifecycle_or_model_profile(bridge, monkeypatch):
    from chef.runtimes.hermes import native, research

    host = native.NativeWorkers(None)
    checks = []
    platform_check = host.check_tool_execution

    def check_tools():
        checks.append(True)
        if os.name == "posix":
            platform_check()

    monkeypatch.setattr(host, "check_tool_execution", check_tools)
    monkeypatch.setattr(research, "profile_config", lambda: pytest.fail("attachment read Hermes model profile"))
    result = workers.status({"workers": bridge()}, native=host)
    assert result == {"backend": "garden-bridge", "roles": ["chef:researcher", "chef:dev-small"], "available": True}
    assert checks == [True]


def test_claude_status_and_dispatch_forward_same_private_settings_path(project, tmp_path, monkeypatch):
    from chef.runtimes.hermes import claude

    settings_path = str(tmp_path / "private-routing.json")
    settings = {"backend": "claude", "claudeSettings": settings_path,
                "claudeRoles": {"dev-small": {"model": "imaginary-model"}}}
    configure(project, settings)
    probed, delegated = [], []
    monkeypatch.setattr(claude, "executable", lambda **kwargs: (probed.append(kwargs) or "/imaginary/claude"))
    monkeypatch.setattr(claude, "run", lambda *args, **kwargs: (delegated.append((args, kwargs)) or {"ok": True}))
    adapter = json.loads(project.adapter.read_text(encoding="utf-8"))
    assert workers.status(adapter, cwd=str(project.root))["available"]
    assert run(project, FakeNative(project)) == {"ok": True}
    assert probed == [{"cwd": str(project.root), "settings_path": settings_path}]
    args, kwargs = delegated[0]
    assert args[3] == str(project.root)
    assert kwargs["settings_path"] == settings_path
    assert kwargs["settings"] == settings["claudeRoles"]


@pytest.mark.parametrize("mode,error", [("bad-status", "not JSON"), ("invalid", "invalid worker attachment tool"),
                                        ("duplicate", "invalid worker attachment tool"),
                                        ("stderr", "stderr output limit"), ("large-line", "JSONL size limit")])
def test_attachment_rejects_invalid_protocol_without_fallback(project, bridge, mode, error):
    configure(project, bridge(mode))
    native = FakeNative(project)
    with pytest.raises(ValueError, match=error):
        run(project, native)
    assert len(native.calls) <= (1 if mode == "duplicate" else 0)
    assert native.revoked


def test_readonly_attachment_cannot_gain_write_capability(project, bridge):
    configure(project, bridge("readonly"))
    result = run(project, FakeNative(project), role="researcher")
    assert result["ok"] is False and "unavailable to the assigned role" in result["error"]


def test_attachment_missing_advertised_role_does_not_start_tools(project, bridge):
    configure(project, bridge())
    native = FakeNative(project)
    with pytest.raises(ValueError, match="does not support"):
        run(project, native, role="verifier")
    assert not native.calls


@pytest.mark.parametrize("value", [None, "missing", [], {}, {"command": ["relative-agent"]},
                                    {"command": ["/imaginary/bridge"], "fallback": "codex"}])
def test_attachment_configuration_is_explicit_and_cannot_enable_fallback(value):
    with pytest.raises(ValueError):
        workers.backend_for({"workers": {"backend": "garden-bridge", "attachments": {"garden-bridge": value}}})


def test_shared_workspace_gate_rejects_overlapping_native_and_attachment_dispatch(project, bridge):
    native = FakeNative(project)
    observed = []

    def first(*args, **kwargs):
        configure(project, bridge())
        with pytest.raises(LockBusy):
            run(project, native)
        observed.append(True)
        return {"ok": True}

    native.run = first
    assert run(project, native)["ok"] and observed == [True]


def test_native_dispatch_does_not_accept_protected_state_mutation(project):
    native = FakeNative(project)

    def mutate(*args, **kwargs):
        (project.state / "orchard-mission/mission.md").write_text("changed", encoding="utf-8")
        return {"ok": True}

    native.run = mutate
    with pytest.raises(ValueError, match="protected mission state changed"):
        run(project, native)
    assert (project.state / "orchard-mission/mission.md").read_text(encoding="utf-8") == "changed"


def test_attachment_timeout_revokes_assignment_and_reaps_bridge(project, bridge, tmp_path):
    marker = tmp_path / "bridge.pid"
    native = FakeNative(project)
    selected = attachment.Attachment(bridge("sleep", marker), "garden-bridge")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        selected.run(native, "dev-small", "Wait", 1, str(project.root), str(project.state), project.active, None, None)
    assert time.monotonic() - started < 4 and native.revoked
    assert marker.is_file()
    assert not workers.process_alive(int(marker.read_text()))


def test_attachment_blocked_stdin_respects_deadline_and_revokes_assignment(project, bridge):
    native = FakeNative(project)
    selected = attachment.Attachment(bridge("no-read"), "garden-bridge")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out writing"):
        selected.run(native, "dev-small", "x" * 100000, 1, str(project.root), str(project.state), project.active, None, None)
    assert time.monotonic() - started < 4 and native.revoked


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires macOS/Linux")
def test_attachment_timeout_kills_term_ignoring_descendant(project, bridge, tmp_path):
    leader, child = tmp_path / "bridge.pid", tmp_path / "child.pid"
    native = FakeNative(project)
    selected = attachment.Attachment(bridge("sleep-child", leader, child), "garden-bridge")
    with pytest.raises(TimeoutError):
        selected.run(native, "dev-small", "Wait", 1, str(project.root), str(project.state), project.active, None, None)
    assert native.revoked and leader.is_file() and child.is_file()
    assert not workers.process_alive(int(leader.read_text()))
    # POSIX may retain a killed grandchild as a zombie until its init reaps it.
    import subprocess

    status = subprocess.run(["ps", "-o", "stat=", "-p", child.read_text()], capture_output=True,
                            text=True, timeout=5).stdout.strip()
    assert not status or status.startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="native shell execution requires macOS/Linux")
def test_attachment_real_command_execution_and_guard_denial(project, bridge):
    from chef.runtimes.hermes.native import NativeWorkers

    native = NativeWorkers(None)
    configure(project, bridge("command"))
    result = run(project, native)
    assert result["ok"]
    assert (project.root / "note.txt").read_text() == "attached"
    configure(project, bridge("denied"))
    result = run(project, native)
    assert not result["ok"] and "fictional policy" in result["error"]
