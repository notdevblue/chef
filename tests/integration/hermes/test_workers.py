"""Hermes role worker authority and hook acknowledgements with fictional homes."""

import io
import contextlib
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.guards import adapters, mission, subagents
from chef.runtimes import hooks
from chef.runtimes.hermes import workers


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "garden with spaces"
    root.mkdir()
    selected = root / ".hermes/orchestrator/adapter.json"
    write_json(selected, {
        "schema": 1, "project": "fictional-orchard", "paths": {"stateDir": "state"},
        "agents": {"allow": ["chef:*"]},
        "guard": {"deny": [{"pattern": "forbidden-action", "reason": "fictional command blocked"}]},
    })
    active = root / "state/ACTIVE"
    write_json(active, {
        "id": "garden-mission", "session": "garden-owner", "runtime": "hermes",
        "openedAt": "2026-01-01",
    })
    (root / "state/garden-mission").mkdir()
    (root / "state/garden-mission/mission.md").write_text("# Fictional mission\n")
    for name in ("HAN_ADAPTERS_DIR", "HAN_REPO", "CLAUDE_PROJECT_DIR", "GROK_SESSION_ID",
                 "GROK_HOOK_EVENT", "CHEF_HERMES_WORKER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(adapters, "in_place_adapters_dir", lambda: str(tmp_path / "absent"))
    return SimpleNamespace(root=root, adapter=selected, active=active)


@pytest.fixture
def codex(monkeypatch):
    auth_calls = []
    worker_calls = []

    def auth(command, **kwargs):
        auth_calls.append((command, kwargs))
        assert command == ["/fictional/codex", "login", "status"]
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")

    def run(command, prompt, env, timeout):
        context_path = Path(env[workers.WORKER_ENV])
        data = json.loads(context_path.read_text(encoding="utf-8"))
        worker_calls.append((command, prompt, dict(env), timeout, data))
        with monkeypatch.context() as context:
            context.setenv(workers.WORKER_ENV, str(context_path))
            normalized = hooks.normalize_payload({
                "han_runtime": "codex", "hook_event_name": "SessionStart",
                "session_id": "native-codex-worker-session", "cwd": data["cwd"],
            })
            probe_payload = {
                "session_id": "native-codex-worker-session", "cwd": data["cwd"],
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo han-mission open chef-worker-probe"},
            }
            output = io.StringIO()
            context.setattr(mission.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(probe_payload).encode())))
            context.setattr(mission.sys, "stdout", output)
            assert mission.main("codex") == 0
            assert json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert normalized["session_id"] == "garden-owner"
        assert normalized["agent_id"] == "chef-codex-worker"
        assert normalized["han_hermes_worker"]["digest"] == workers.implementation_digest()
        return SimpleNamespace(returncode=0, stdout='{"type":"item.completed","item":{"type":"agent_message","text":"fictional result"}}\n', stderr="")

    monkeypatch.setattr(workers.shutil, "which", lambda name: "/fictional/codex" if name == "codex" else None)
    monkeypatch.setattr(workers.subprocess, "run", auth)
    monkeypatch.setattr(workers, "run_codex", run)
    return SimpleNamespace(auth_calls=auth_calls, calls=worker_calls, auth=auth, run=run)


def delegate(project, **args):
    return workers.delegate({"role": "chef:dev-small", "prompt": "Inspect the fictional garden.", **args},
                            session_id="garden-owner", cwd=str(project.root))


def test_worker_requires_subscription_and_matching_hook_probe(project, codex, monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.setenv(name, "fictional-unusable-value")
    prompt = 'Keep quotes "and spaces"; $HOME stays data.\nSecond line.'
    result = delegate(project, prompt=prompt, timeout=240)
    assert result == {"ok": True, "runtime": "codex", "role": "chef:dev-small",
                      "exit_code": 0, "result": "fictional result", "error": ""}
    assert len(codex.auth_calls) == 1 and len(codex.calls) == 2
    probe, worker = codex.calls
    assert probe[0][probe[0].index("--sandbox") + 1] == "read-only"
    assert probe[3] == 120
    assert worker[0][worker[0].index("--sandbox") + 1] == "workspace-write"
    assert worker[1] == prompt and prompt not in worker[0]
    assert worker[3] == 240
    assert probe[4]["session"] == worker[4]["session"] == "garden-owner"
    assert probe[4]["cwd"] == str(project.root.resolve())
    for call in (probe, worker):
        assert all(name not in call[2] for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"))
        assert call[2]["PWD"] == str(project.root)
    assert not Path(worker[2][workers.WORKER_ENV]).exists()
    assert mission.read_active(str(project.root / "state")).session == "garden-owner"


def test_worker_hook_process_inherits_request_profile(project, codex, monkeypatch):
    gateway = Path(os.environ["HERMES_HOME"])
    profile = gateway / "profiles" / "garden"
    registry = profile / "chef" / "adapters"
    selected = registry / "garden" / "adapter.json"
    selected.parent.mkdir(parents=True)
    project.adapter.rename(selected)
    write_json(registry / "projects.json", [{"adapter": "garden", "roots": [str(project.root)]}])
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: profile))
    assert delegate(project)["ok"]
    for _, _, env, _, _ in codex.calls:
        assert env["HERMES_HOME"] == str(profile)
        with monkeypatch.context() as child:
            child.delitem(sys.modules, "hermes_constants")
            child.setenv("HERMES_HOME", env["HERMES_HOME"])
            located = adapters.find_adapter({"cwd": str(project.root), "han_runtime": "hermes"})
            assert Path(located.adapter_path) == selected
    assert os.environ["HERMES_HOME"] == str(gateway)


def test_uninitialized_mission_is_rejected_before_any_model_or_auth_call(project, codex):
    (project.root / "state/garden-mission/mission.md").unlink()
    with pytest.raises(ValueError, match="initialize"):
        delegate(project)
    assert not codex.calls and not codex.auth_calls


def test_opaque_helper_state_mutation_invalidates_worker_result_without_rollback(project, codex, monkeypatch):
    control = project.root / "state/garden-mission/units.tsv"
    control.parent.mkdir(exist_ok=True)
    control.write_text("original")
    calls = []

    def run(command, prompt, env, timeout):
        result = codex.run(command, prompt, env, timeout)
        calls.append(command)
        if len(calls) == 2:
            control.write_text("unexpected helper mutation")
        return result

    monkeypatch.setattr(workers, "run_codex", run)
    with pytest.raises(ValueError, match="protected mission state changed"):
        delegate(project)
    assert control.read_text() == "unexpected helper mutation"


@pytest.mark.parametrize("role", workers.ROLES)
def test_role_command_preserves_model_instructions_and_native_argument_boundaries(project, role):
    command = workers.command_for("/fictional/codex", role, str(project.root))
    assert command[:3] == ["/fictional/codex", "--no-daemon", "exec"]
    source = tomllib.loads((workers.PLUGIN_ROOT / "codex/agents" / f"chef-{role}.toml").read_text(encoding="utf-8"))
    assert command[command.index("-C") + 1] == str(project.root)
    if source.get("model"):
        assert command[command.index("--model") + 1] == source["model"]
    else:
        assert "--model" not in command
    assert command[command.index("--sandbox") + 1] == ("read-only" if role in workers.READ_ONLY else "workspace-write")
    assert command[-1] == "-"
    settings = dict(command[index + 1].split("=", 1) for index, item in enumerate(command) if item == "-c")
    assert json.loads(settings["forced_login_method"]) == "chatgpt"
    assert json.loads(settings["model_provider"]) == "openai"
    if source.get("model_reasoning_effort"):
        assert json.loads(settings["model_reasoning_effort"]) == source["model_reasoning_effort"]
    assert json.loads(settings["developer_instructions"]).startswith(source["developer_instructions"])
    assert "--enable" in command and command[command.index("--enable") + 1] == "hooks"


@pytest.mark.parametrize("status,stdout,stderr", [
    (1, "", "Not logged in"),
    (0, "Logged in using an API key", ""),
])
def test_missing_subscription_never_starts_any_worker(project, codex, monkeypatch, status, stdout, stderr):
    monkeypatch.setattr(workers.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=status, stdout=stdout, stderr=stderr))
    with pytest.raises(ValueError, match="subscription"):
        delegate(project)
    assert not codex.calls


def test_missing_codex_does_not_fall_back_to_an_api(project, codex, monkeypatch):
    monkeypatch.setattr(workers.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="Codex CLI"):
        delegate(project)
    assert not codex.auth_calls and not codex.calls


@pytest.mark.parametrize("mutation", ["missing", "wrong-owner", "wrong-runtime", "corrupt"])
def test_worker_refuses_missing_or_foreign_active_before_auth(project, codex, mutation):
    if mutation == "missing":
        project.active.unlink()
    elif mutation == "corrupt":
        project.active.write_text("{", encoding="utf-8")
    else:
        active = json.loads(project.active.read_text(encoding="utf-8"))
        active["session" if mutation == "wrong-owner" else "runtime"] = "another-session"
        write_json(project.active, active)
    with pytest.raises(ValueError, match="ACTIVE"):
        delegate(project)
    assert not codex.auth_calls and not codex.calls


def test_disallowed_role_does_not_authenticate_or_launch(project, codex):
    adapter = json.loads(project.adapter.read_text(encoding="utf-8"))
    adapter["agents"]["allow"] = ["chef:researcher"]
    write_json(project.adapter, adapter)
    with pytest.raises(ValueError, match="permitted"):
        delegate(project)
    assert not codex.auth_calls and not codex.calls


PROJECT_HOOK = 'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command="fictional-guard hook"}]}]'


def test_adapter_worker_arguments_reach_probe_and_worker_in_order(project, codex):
    adapter = json.loads(project.adapter.read_text(encoding="utf-8"))
    extra = ["-c", "sandbox_workspace_write.network_access=true", "-c", 'shell_environment_policy.set.ORCHARD="1"']
    adapter["runtimes"] = {"hermes": {"workers": {"codexArguments": extra}}}
    write_json(project.adapter, adapter)
    assert delegate(project)["ok"]
    assert len(codex.calls) == 2
    for command, *_ in codex.calls:
        assert command[-len(extra) - 1:] == extra + ["-"]
        assert command[command.index("-C") + 1] == str(project.root)


def test_worker_arguments_default_to_none(project):
    assert workers.extra_arguments({}) == []
    assert workers.command_for("/fictional/codex", "dev-small", str(project.root))[-1] == "-"


@pytest.mark.parametrize("workers_value", [
    [],
    {"codexArguments": "-c a=b"},
    {"codexArguments": ["-c"]},
    {"codexArguments": ["--sandbox", "danger-full-access"]},
    {"codexArguments": ["-c", "no-assignment"]},
    {"codexArguments": ["-c", "=value"]},
    {"codexArguments": ["-c", "a=b\nc=d"]},
    {"codexArguments": ["-c", 'model="other"']},
    {"codexArguments": ["-c", 'model_providers.other.base_url="https://example.invalid"']},
    {"codexArguments": ["-c", "features.hooks=false"]},
    {"codexArguments": ["-c", 'sandbox_mode="danger-full-access"']},
    {"codexArguments": ["-c", 'approval_policy="never"']},
    {"codexArguments": ["-c", 'developer_instructions="replace"']},
    {"codexArguments": ["-c", PROJECT_HOOK]},
    {"codexArguments": ["-c", 'mcp_servers.orchard.command="fictional"']},
    {"codexArguments": ["-c", 'sandbox_workspace_write.writable_roots=["/fictional"]']},
    {"guardCommand": "fictional-guard"},
    {"guardCommand": []},
    {"guardCommand": ["fictional-guard", ""]},
    {"guardCommand": [str(Path("/fictional/missing-guard"))]},
])
def test_invalid_or_pinned_worker_arguments_stop_before_auth(project, codex, workers_value):
    adapter = json.loads(project.adapter.read_text(encoding="utf-8"))
    adapter["workers"] = workers_value
    write_json(project.adapter, adapter)
    with pytest.raises(ValueError, match="workers"):
        delegate(project)
    assert not codex.auth_calls and not codex.calls


@pytest.mark.parametrize("acknowledgement,returncode", [(None, 0), ("outdated-digest", 0), ("matching", 1)])
def test_missing_or_failed_probe_prevents_write_worker(project, codex, monkeypatch, acknowledgement, returncode):
    calls = []

    def probe(command, prompt, env, timeout):
        calls.append(command)
        data = json.loads(Path(env[workers.WORKER_ENV]).read_text(encoding="utf-8"))
        if acknowledgement:
            Path(data["ready"]).write_text(data["digest"] if acknowledgement == "matching" else acknowledgement)
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(workers, "run_codex", probe)
    with pytest.raises(ValueError, match="hooks"):
        delegate(project)
    assert len(calls) == 1
    assert calls[0][calls[0].index("--sandbox") + 1] == "read-only"


def test_failed_write_worker_returns_failure_without_fallback(project, codex, monkeypatch):
    def run(command, prompt, env, timeout):
        result = codex.run(command, prompt, env, timeout)
        if len(codex.calls) == 2:
            result.returncode, result.stderr = 9, "fictional failure"
        return result

    monkeypatch.setattr(workers, "run_codex", run)
    result = delegate(project)
    assert result["ok"] is False and result["exit_code"] == 9
    assert result["error"] == "fictional failure"
    assert len(codex.calls) == 2


def test_owner_change_during_read_only_probe_prevents_write_worker(project, codex, monkeypatch):
    def run(command, prompt, env, timeout):
        result = codex.run(command, prompt, env, timeout)
        active = json.loads(project.active.read_text(encoding="utf-8"))
        active["session"] = "replacement-owner"
        write_json(project.active, active)
        return result

    monkeypatch.setattr(workers, "run_codex", run)
    with pytest.raises(ValueError, match="changed"):
        delegate(project)
    assert len(codex.calls) == 1


def test_actual_worker_must_acknowledge_hook_after_probe(project, codex, monkeypatch):
    calls = []

    def run(command, prompt, env, timeout):
        calls.append(command)
        if len(calls) == 1:
            return codex.run(command, prompt, env, timeout)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(workers, "run_codex", run)
    with pytest.raises(ValueError, match="acknowledge"):
        delegate(project)
    assert len(calls) == 2


def context(project, monkeypatch, **updates):
    record = project.root.parent / "trusted-worker.json"
    data = {
        "schema": 1, "pid": os.getpid(), "session": "garden-owner", "role": "dev-small",
        "mission": "garden-mission",
        "cwd": str(project.root.resolve()), "state": str((project.root / "state").resolve()),
        "ready": str(record.with_suffix(".ready")),
        "guard_ready": str(record.with_suffix(".guard-ready")), "digest": workers.implementation_digest(),
        "guard": None,
        **updates,
    }
    write_json(record, data)
    monkeypatch.setenv(workers.WORKER_ENV, str(record))
    return data


@pytest.mark.parametrize("tool,inputs", [
    ("Bash", {"command": "echo han-mission close garden-mission"}),
    ("functions.spawn_agent", {"agent_type": "default"}),
    ("Write", {"file_path": "state/ACTIVE", "content": "replacement"}),
    ("Bash", {"command": "printf replacement > state/ACTIVE"}),
    ("Bash", {"command": "python -c \"from pathlib import Path; Path('state/ACTIVE').write_text('replacement')\""}),
])
def test_actual_guard_blocks_worker_marker_nested_agent_and_state_write(project, monkeypatch, tool, inputs):
    data = context(project, monkeypatch)
    payload = hooks.normalize_payload({
        "han_runtime": "codex", "hook_event_name": "PreToolUse", "session_id": "codex-child",
        "cwd": str(project.root), "tool_name": tool, "tool_input": inputs,
    })
    assert payload["session_id"] == data["session"]
    assert payload["han_hermes_worker"]["role"] == "dev-small"
    outcome = subagents.decide(payload) if tool.endswith("spawn_agent") else mission.decide(payload)
    assert outcome.deny
    assert mission.read_active(str(project.root / "state")).session == "garden-owner"


def test_worker_uses_hermes_only_adapter_for_project_command_guard(project, monkeypatch):
    context(project, monkeypatch)
    payload = hooks.normalize_payload({
        "han_runtime": "codex", "hook_event_name": "PreToolUse", "session_id": "codex-child",
        "cwd": str(project.root), "tool_name": "Bash", "tool_input": {"command": "forbidden-action"},
    })
    assert payload["han_runtime"] == "hermes"
    assert mission.decide(payload).deny.startswith("fictional command blocked")


def test_worker_cannot_escape_assigned_workspace_through_cwd(project, monkeypatch):
    context(project, monkeypatch)
    with pytest.raises(ValueError, match="escaped"):
        hooks.normalize_payload({"han_runtime": "codex", "hook_event_name": "PreToolUse",
                                 "cwd": str(project.root.parent)})


def test_worker_filesystem_case_alias_keeps_ownership_and_state_protection(project, monkeypatch):
    alias = project.root.with_name(project.root.name.upper())
    if not alias.exists() or not os.path.samefile(alias, project.root):
        pytest.skip("requires a case-insensitive filesystem")
    context(project, monkeypatch)
    normalized = hooks.normalize_payload({"han_runtime": "codex", "hook_event_name": "PreToolUse",
        "cwd": str(alias), "tool_name": "Write", "tool_input": {"file_path": "STATE/ACTIVE"}})
    assert workers.worker_denial(normalized)
    assert normalized["session_id"] == "garden-owner"


def test_worker_lock_identity_is_shared_by_filesystem_aliases(project, codex, monkeypatch):
    alias = project.root.with_name(project.root.name.upper())
    if not alias.exists() or not os.path.samefile(alias, project.root):
        pytest.skip("requires a case-insensitive filesystem")
    locks = []
    def lock(path, name):
        locks.append(path)
        return contextlib.nullcontext()
    monkeypatch.setattr(workers, "FileLock", lock)
    delegate(project)
    workers.delegate({"role": "chef:dev-small", "prompt": "Inspect the fictional garden."},
                     session_id="garden-owner", cwd=str(alias))
    assert len(locks) == 2 and locks[0] == locks[1]


def test_each_worker_hook_revalidates_active_ownership(project, monkeypatch):
    context(project, monkeypatch)
    active = json.loads(project.active.read_text(encoding="utf-8"))
    active["session"] = "replacement-owner"
    write_json(project.active, active)
    with pytest.raises(ValueError, match="ownership"):
        hooks.normalize_payload({"han_runtime": "codex", "hook_event_name": "PreToolUse",
                                 "cwd": str(project.root)})


@pytest.mark.parametrize("change", ["expired", "mismatched-digest"])
def test_hook_context_must_have_live_owner_and_matching_deployment(project, monkeypatch, change):
    context(project, monkeypatch, **({"digest": "outdated"} if change == "mismatched-digest" else {}))
    if change == "expired":
        monkeypatch.setattr(workers, "process_alive", lambda _: False)
    with pytest.raises(ValueError, match="expired|differs"):
        hooks.normalize_payload({"han_runtime": "codex", "hook_event_name": "PreToolUse"})


def test_actual_worker_hook_fails_closed_for_broken_context(project, monkeypatch):
    record = project.root.parent / "broken-context.json"
    record.write_text("{", encoding="utf-8")
    monkeypatch.setenv(workers.WORKER_ENV, str(record))
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "echo harmless"}, "cwd": str(project.root)}
    output = io.StringIO()
    monkeypatch.setattr(mission.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(mission.sys, "stdout", output)
    assert mission.main("codex") == 0
    assert json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_codex_passes_prompt_on_stdin_and_explicit_cwd(project, monkeypatch):
    seen = []

    class Process:
        returncode = 0

        def communicate(self, **kwargs):
            seen.append(("communicate", kwargs))
            return "fictional output", ""

    def popen(*args, **kwargs):
        seen.append((args, kwargs))
        return Process()

    monkeypatch.setattr(workers.subprocess, "Popen", popen)
    command = workers.command_for("/fictional/codex", "dev-small", str(project.root))
    result = workers.run_codex(command, "prompt; $DATA\ntext", {"PWD": str(project.root)}, 60)
    assert result.returncode == 0 and result.stdout == "fictional output"
    positional, keywords = seen[0]
    assert positional == (command,)
    assert keywords["cwd"] == str(project.root)
    assert not keywords.get("shell", False)
    assert keywords["start_new_session"] == (os.name != "nt")
    assert seen[1] == ("communicate", {"input": "prompt; $DATA\ntext", "timeout": 60})


@pytest.mark.skipif(os.name == "nt", reason="POSIX Hermes process group cleanup")
@pytest.mark.parametrize("cancel", [False, True])
def test_timed_out_or_cancelled_worker_stops_its_term_ignoring_descendant(tmp_path, monkeypatch, cancel):
    child = tmp_path / "child.py"
    child.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    original_popen = subprocess.Popen
    created = []

    class CancelledProcess:
        def __init__(self, process):
            self.process = process
            self.cancelled = False

        def __getattr__(self, name):
            return getattr(self.process, name)

        def communicate(self, **kwargs):
            if not self.cancelled:
                self.cancelled = True
                deadline = time.monotonic() + 5
                while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                raise KeyboardInterrupt("fictional cancellation")
            return self.process.communicate(**kwargs)

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        created.append(process)
        return CancelledProcess(process) if cancel else process

    command = [sys.executable, str(parent), "-C", str(tmp_path)]
    with monkeypatch.context() as context:
        context.setattr(workers.subprocess, "Popen", popen)
        with pytest.raises(KeyboardInterrupt if cancel else subprocess.TimeoutExpired):
            workers.run_codex(command, "", os.environ.copy(), 1)
    assert len(created) == 1 and created[0].returncode is not None
    child_pid = int((tmp_path / "child.pid").read_text())
    # A killed orphan may briefly remain a zombie before init reaps it; it must
    # not remain a running descendant after run_codex has returned.
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True,
                            text=True, timeout=5).stdout.strip()
    assert not status or status.startswith("Z"), status


GUARD_SCRIPT = """
import json, pathlib, sys, time
payload = json.load(sys.stdin)
pathlib.Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")
command = (payload.get("tool_input") or {}).get("command", "")
if "sleep" in command:
    time.sleep(5)
if "crash" in command:
    sys.exit(3)
if "noise" in command:
    print("not json")
if "deny" in command:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "fictional project rule"}}))
"""


def guarded_project(project, tmp_path):
    script = tmp_path / "project_guard.py"
    script.write_text(GUARD_SCRIPT, encoding="utf-8")
    seen = tmp_path / "seen.json"
    argv = [sys.executable, str(script), str(seen)]
    adapter = json.loads(project.adapter.read_text(encoding="utf-8"))
    adapter["runtimes"] = {"hermes": {"workers": {"guardCommand": argv}}}
    write_json(project.adapter, adapter)
    return seen, argv


def worker_call(project, command):
    return mission.decide({
        "han_runtime": "codex", "hook_event_name": "PreToolUse", "session_id": "codex-child",
        "cwd": str(project.root), "tool_name": "Bash", "tool_input": {"command": command},
    })


def test_project_guard_command_sees_owner_identity_and_can_deny(project, monkeypatch, tmp_path):
    seen, argv = guarded_project(project, tmp_path)
    context(project, monkeypatch, guard=argv)
    assert not worker_call(project, "echo allowed").deny
    payload = json.loads(seen.read_text(encoding="utf-8"))
    assert (payload["session_id"], payload["agent_id"], payload["han_runtime"]) == ("garden-owner", "chef-codex-worker", "hermes")
    assert payload["tool_input"] == {"command": "echo allowed"}
    assert "han_hermes_worker" not in payload
    assert worker_call(project, "echo deny").deny == "프로젝트 워커 가드: fictional project rule"


@pytest.mark.parametrize("command,reason", [("echo crash", "실패"), ("echo noise", "JSON"), ("echo sleep", "실행하지 못함")])
def test_project_guard_failures_deny_the_worker_call(project, monkeypatch, tmp_path, command, reason):
    _, argv = guarded_project(project, tmp_path)
    context(project, monkeypatch, guard=argv)
    monkeypatch.setattr(workers, "GUARD_TIMEOUT", 1)
    assert reason in worker_call(project, command).deny


def test_project_guard_is_not_run_outside_hermes_workers(project, monkeypatch, tmp_path):
    seen, _ = guarded_project(project, tmp_path)
    assert not worker_call(project, "echo deny").deny
    assert not seen.exists()


def test_worker_with_project_guard_still_reaches_the_role_worker(project, codex, tmp_path):
    _, argv = guarded_project(project, tmp_path)
    assert delegate(project)["ok"]
    assert len(codex.calls) == 2
    assert all(call[4]["guard"] == argv for call in codex.calls)


def test_planted_adapter_cannot_drop_the_fixed_project_guard(project, monkeypatch, tmp_path):
    seen, argv = guarded_project(project, tmp_path)
    context(project, monkeypatch, guard=argv)
    # A worker writes a nearer adapter without workers.guardCommand; the hook keeps the fixed guard.
    write_json(project.root / ".hermes/orchestrator/adapter.json", {"schema": 1, "project": "planted", "paths": {"stateDir": "state"}})
    assert worker_call(project, "echo deny").deny == "프로젝트 워커 가드: fictional project rule"
    assert seen.exists()


def test_context_without_a_fixed_guard_entry_fails_closed(project, monkeypatch):
    data = context(project, monkeypatch)
    data.pop("guard")
    write_json(Path(os.environ[workers.WORKER_ENV]), data)
    with pytest.raises(ValueError, match="update Chef"):
        worker_call(project, "echo allowed")
    # The hook entry turns any worker-context error into a denial.
    output = io.StringIO()
    payload = {"session_id": "codex-child", "cwd": str(project.root), "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": "echo allowed"}}
    monkeypatch.setattr(mission.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(mission.sys, "stdout", output)
    assert mission.main("codex") == 0
    assert json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"
