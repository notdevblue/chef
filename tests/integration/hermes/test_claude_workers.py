"""Claude workers using fictional projects and fake CLI I/O."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.guards import adapters, mission
from chef.runtimes import hooks
from chef.runtimes.hermes import claude, workers


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "fictional orchard"
    root.mkdir()
    write_json(root / ".hermes/orchestrator/adapter.json", {
        "schema": 1, "project": "fictional-orchard", "paths": {"stateDir": "state"},
        "agents": {"allow": ["chef:*"]}, "workers": {"backend": "claude"}})
    write_json(root / "state/ACTIVE", {"id": "orchard-mission", "session": "orchard-owner",
                                     "runtime": "hermes", "openedAt": "2026-01-01"})
    folder = root / "state/orchard-mission"
    folder.mkdir()
    (folder / "mission.md").write_text("# Fictional mission\n")
    for name in ("HAN_ADAPTERS_DIR", "HAN_REPO", "CHEF_HERMES_WORKER", "GROK_SESSION_ID", "GROK_HOOK_EVENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(adapters, "in_place_adapters_dir", lambda: str(tmp_path / "missing"))
    return SimpleNamespace(root=root, state=str(root / "state"), active=mission.read_active(str(root / "state")))


@pytest.fixture
def cli(monkeypatch):
    calls = []
    monkeypatch.setattr(claude, "checked_executable", lambda *a, **kw: "/fictional/claude")

    def hook(command, payload, env, cwd):
        settings = json.loads(Path(command[command.index("--settings") + 1]).read_text(encoding="utf-8"))
        spec = settings["hooks"][payload["hook_event_name"]][0]["hooks"][0]
        result = subprocess.run([spec["command"], *spec["args"]], cwd=cwd, env=env,
                                input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout or "{}")

    def execute(command, prompt, env, timeout, cwd):
        data = json.loads(Path(env[claude.WORKER_ENV]).read_text(encoding="utf-8"))
        snapshot = json.loads(Path(command[command.index("--settings") + 1]).read_text(encoding="utf-8"))
        calls.append((command, prompt, env, timeout, cwd, data, snapshot))
        payload = {"session_id": "separate-cli-session", "cwd": cwd, "hook_event_name": "SessionStart"}
        hook(command, payload, env, cwd)
        if data["phase"] == "probe":
            output = hook(command, {**payload, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                    "tool_input": {"command": claude.PROBE}}, env, cwd)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "result", "subtype": "success",
                                                                 "is_error": False, "result": "observed result"}), "")

    monkeypatch.setattr(claude, "run_process", execute)
    return SimpleNamespace(calls=calls, execute=execute, hook=hook)


def run(project, role="dev-small", **kwargs):
    return claude.run(role, 'Inspect "quoted" fictional files.\nNext line.', 240, str(project.root),
                      project.state, project.active, None, **kwargs)


def test_per_call_fixed_hooks_probe_and_context_cleanup(project, cli, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, "fictional-selector")
    assert run(project) == {"ok": True, "runtime": "claude", "role": "chef:dev-small", "exit_code": 0,
                            "result": "observed result", "error": ""}
    assert len(cli.calls) == 2
    probe, work = cli.calls
    assert probe[3] == 120 and work[3] == 240
    assert probe[5]["phase"] == "probe" and work[5]["phase"] == "work"
    assert work[5]["session"] == "orchard-owner"
    assert work[5]["backend"] == "claude"
    assert work[1] not in work[0] and work[4] == str(project.root)
    for command, _, env, _, _, _, settings in cli.calls:
        assert env["PWD"] == str(project.root)
        assert env["ANTHROPIC_API_KEY"] == "fictional-selector"
        assert env["CLAUDE_CONFIG_DIR"] == "fictional-selector"
        assert command[command.index("--setting-sources") + 1] == ""
        assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
        assert command[command.index("--disallowedTools") + 1] == "Agent,Task"
        assert "--no-session-persistence" in command and "--strict-mcp-config" in command
        assert settings["disableAllHooks"] is False
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["onFailure"] == "block"
    assert not Path(work[2][claude.WORKER_ENV]).exists()


@pytest.mark.parametrize("role", claude.READ_ONLY)
def test_readonly_roles_have_no_shell_and_use_own_role_defaults(project, cli, role):
    assert run(project, role)["ok"]
    command = cli.calls[-1][0]
    assert "Bash" not in command[command.index("--tools") + 1]
    source = (claude.PLUGIN_ROOT / "agents" / (role + ".md")).read_text(encoding="utf-8")
    config = claude.role_config(role)
    assert config["model"] in source and config["effort"] in source
    assert source.split("---", 2)[2].strip() in config["instructions"]


@pytest.mark.parametrize("value", [{"loggedIn": False}, {}, [], None])
def test_auth_rejects_logged_out_or_malformed_status(monkeypatch, value):
    monkeypatch.setattr(claude, "selected_settings", lambda *args: [])
    monkeypatch.setattr(claude.shutil, "which", lambda _: "/fictional/claude")
    monkeypatch.setattr(claude.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(
        command, 0, "2.1.331 (Claude Code)" if "--version" in command else json.dumps(value), ""))
    with pytest.raises(ValueError, match="authentication"):
        claude.executable()


@pytest.mark.parametrize("method,provider", [("claude.ai", "firstParty"), ("api_key", "firstParty"), ("provider", "bedrock")])
def test_auth_uses_json_status_and_preserves_operator_provider(monkeypatch, method, provider):
    monkeypatch.setattr(claude, "selected_settings", lambda *args: [])
    monkeypatch.setattr(claude.shutil, "which", lambda _: "/fictional/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fictional-unusable-key")
    def auth(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "2.1.331 (Claude Code)", "")
        assert command[-3:] == ["auth", "status", "--json"]
        assert kwargs["env"]["ANTHROPIC_API_KEY"] == "fictional-unusable-key"
        return subprocess.CompletedProcess(command, 0, json.dumps({"loggedIn": True, "authMethod": method, "apiProvider": provider}), "")
    monkeypatch.setattr(claude.subprocess, "run", auth)
    assert claude.executable() == "/fictional/claude"


@pytest.mark.parametrize("version", ["2.1.294 (Claude Code)", "2.0.500", "unknown", ""])
def test_older_or_unknown_cli_version_is_rejected_before_auth(monkeypatch, version):
    monkeypatch.setattr(claude.shutil, "which", lambda _: "/fictional/claude")
    def check(command, **kwargs):
        assert command == ["/fictional/claude", "--version"]
        return subprocess.CompletedProcess(command, 0, version, "")
    monkeypatch.setattr(claude.subprocess, "run", check)
    with pytest.raises(ValueError, match="2.1.295"):
        claude.executable()


def test_settings_endpoint_selection_requires_explicit_snapshot_before_auth(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    write_json(profile / "settings.json", {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.invalid"}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.setattr(claude.shutil, "which", lambda _: "/fictional/claude")
    def version_only(command, **kwargs):
        assert command[-1] == "--version", "ambiguous settings routing reached authentication"
        return subprocess.CompletedProcess(command, 0, "2.1.331 (Claude Code)", "")
    monkeypatch.setattr(claude.subprocess, "run", version_only)
    with pytest.raises(ValueError, match="provide workers.claudeSettings") as failure:
        claude.executable(cwd=str(tmp_path))
    assert str(profile / "settings.json") in str(failure.value)
    assert "gateway.example.invalid" not in str(failure.value)


def test_relative_profile_is_resolved_from_assigned_cwd(tmp_path):
    paths = claude.selected_settings(str(tmp_path), {"CLAUDE_CONFIG_DIR": "relative-profile"})
    assert paths[0] == tmp_path / "relative-profile/settings.json"


def test_auth_route_identity_mismatch_fails_without_explicit_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "selected_settings", lambda *args: [])
    monkeypatch.setattr(claude.shutil, "which", lambda _: "/fictional/claude")
    def auth(command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "2.1.331", "")
        selected = "--settings" not in command
        value = {"loggedIn": True, "authMethod": "api_key" if selected else "claude.ai", "apiProvider": "firstParty"}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
    monkeypatch.setattr(claude.subprocess, "run", auth)
    with pytest.raises(ValueError, match="authentication differs"):
        claude.executable(cwd=str(tmp_path))


def test_explicit_auth_snapshot_uses_private_file_same_cwd_and_no_argv_secrets(tmp_path, monkeypatch):
    source = tmp_path / "auth.json"
    secret = "fictional-private-credential"
    write_json(source, {"env": {"ANTHROPIC_AUTH_TOKEN": secret}, "apiKeyHelper": "fictional-helper"})
    monkeypatch.setattr(claude.shutil, "which", lambda _: "/fictional/claude")
    observed = []
    def auth(command, **kwargs):
        assert kwargs["cwd"] == str(tmp_path)
        assert not any(secret in item for item in command)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "2.1.331", "")
        if "--settings" in command:
            path = Path(command[command.index("--settings") + 1])
            settings = json.loads(path.read_text(encoding="utf-8"))
            assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == secret
            assert settings["apiKeyHelper"] == "fictional-helper"
            assert settings["disableAllHooks"] is False
            if os.name != "nt":
                assert path.stat().st_mode & 0o077 == 0
            observed.append(path)
        return subprocess.CompletedProcess(command, 0, '{"loggedIn":true,"authMethod":"api_key","apiProvider":"firstParty"}', "")
    monkeypatch.setattr(claude.subprocess, "run", auth)
    assert claude.executable(cwd=str(tmp_path), settings_path=str(source)) == "/fictional/claude"
    assert len(observed) == 1 and not observed[0].exists()


def test_worker_auth_snapshot_is_pinned_for_probe_work_and_removed(project, cli, tmp_path):
    source = tmp_path / "auth.json"
    secret = "fictional-private-credential"
    write_json(source, {"env": {"ANTHROPIC_AUTH_TOKEN": secret}, "modelOverrides": {"fictional-model": "provider-model"}})
    assert run(project, settings_path=str(source))["ok"]
    for argv, _, _, _, _, _, settings in cli.calls:
        assert not any(secret in item for item in argv)
        assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == secret
        assert settings["modelOverrides"] == {"fictional-model": "provider-model"}
        assert not Path(argv[argv.index("--settings") + 1]).exists()


@pytest.mark.parametrize("value", [{"hooks": {}}, {"permissions": {}}, {"env": {"CHEF_HERMES_WORKER": "other"}},
                                   {"modelOverrides": {"model": {"nested": "other"}}}, {"apiKeyHelper": []}])
def test_explicit_auth_subset_rejects_capability_or_invalid_shapes(tmp_path, value):
    source = tmp_path / "auth.json"
    write_json(source, value)
    with pytest.raises(ValueError):
        claude.auth_settings(str(source))


def test_explicit_auth_native_shapes_allow_org_lists(tmp_path):
    source = tmp_path / "auth.json"
    for value in ([], ["fictional-org"]):
        write_json(source, {"forceLoginOrgUUID": value, "forceLoginMethod": "gateway"})
        assert claude.auth_settings(str(source))["forceLoginOrgUUID"] == value


@pytest.mark.parametrize("missing", ["ready", "guard_ready"])
def test_missing_ack_prevents_work_and_removes_context(project, cli, monkeypatch, missing):
    def execute(*args):
        result = cli.execute(*args)
        Path(cli.calls[-1][5][missing]).unlink()
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    with pytest.raises(ValueError, match="preflight"):
        run(project)
    assert len(cli.calls) == 1 and not Path(cli.calls[0][2][claude.WORKER_ENV]).exists()


def test_mission_change_during_probe_prevents_work(project, cli, monkeypatch):
    def execute(*args):
        result = cli.execute(*args)
        write_json(project.root / "state/ACTIVE", {"id": "other-mission", "session": "other-owner", "runtime": "hermes"})
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    with pytest.raises(ValueError, match="mission changed"):
        run(project)
    assert len(cli.calls) == 1


@pytest.mark.parametrize("role,tool,inputs", [
    ("researcher", "Bash", {"command": "echo forbidden"}),
    ("code-reviewer", "Write", {"file_path": "notes.txt"}),
    ("dev-small", "Agent", {"prompt": "nested"}),
    ("dev-small", "Task", {"prompt": "nested"}),
    ("dev-small", "Bash", {"command": "echo bounded", "run_in_background": True}),
    ("dev-small", "Write", {"file_path": "../outside.txt"}),
])
def test_hook_rejects_tools_outside_fixed_role_capabilities(project, cli, monkeypatch, role, tool, inputs):
    def execute(command, prompt, env, timeout, cwd):
        result = cli.execute(command, prompt, env, timeout, cwd)
        if cli.calls[-1][5]["phase"] == "work":
            output = cli.hook(command, {"hook_event_name": "PreToolUse", "cwd": cwd,
                                       "tool_name": tool, "tool_input": inputs}, env, cwd)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    assert run(project, role)["ok"]


@pytest.mark.parametrize("command", [
    "printf changed > ../outside.txt",
    "python -c \"from pathlib import Path; Path('../outside.txt').write_text('changed')\"",
    "cp notes.txt ../outside.txt",
    "mv ../outside.txt renamed.txt",
    "printf changed > .claude/settings.json",
    "printf changed > state/ACTIVE",
])
def test_canonical_hook_denies_supported_shell_writes(project, cli, monkeypatch, command):
    def execute(argv, prompt, env, timeout, cwd):
        result = cli.execute(argv, prompt, env, timeout, cwd)
        if cli.calls[-1][5]["phase"] == "work":
            output = cli.hook(argv, {"hook_event_name": "PreToolUse", "cwd": cwd,
                                    "tool_name": "Bash", "tool_input": {"command": command}}, env, cwd)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    assert run(project)["ok"]
    assert not (project.root.parent / "outside.txt").exists()


@pytest.mark.parametrize("command", ["cat ../outside.txt", "rg needle notes.txt", "printf changed > result.txt"])
def test_canonical_hook_keeps_reads_and_in_workspace_outputs(project, cli, monkeypatch, command):
    def execute(argv, prompt, env, timeout, cwd):
        result = cli.execute(argv, prompt, env, timeout, cwd)
        if cli.calls[-1][5]["phase"] == "work":
            output = cli.hook(argv, {"hook_event_name": "PreToolUse", "cwd": cwd,
                                    "tool_name": "Bash", "tool_input": {"command": command}}, env, cwd)
            assert output.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    assert run(project)["ok"]


def test_canonical_hook_rejects_write_through_workspace_symlink(project, cli, monkeypatch):
    destination = project.root / "actual"
    destination.mkdir()
    try:
        (project.root / "alias").symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create a symlink")
    def execute(argv, prompt, env, timeout, cwd):
        result = cli.execute(argv, prompt, env, timeout, cwd)
        if cli.calls[-1][5]["phase"] == "work":
            output = cli.hook(argv, {"hook_event_name": "PreToolUse", "cwd": cwd,
                                    "tool_name": "Bash", "tool_input": {"command": "printf changed > alias/output.txt"}}, env, cwd)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    assert run(project)["ok"]


def test_cross_runtime_context_reuse_is_denied(project, cli, monkeypatch):
    def execute(command, prompt, env, timeout, cwd):
        result = cli.execute(command, prompt, env, timeout, cwd)
        with monkeypatch.context() as context:
            context.setenv(claude.WORKER_ENV, env[claude.WORKER_ENV])
            with pytest.raises(ValueError, match="runtime does not match"):
                hooks.normalize_payload({"cwd": cwd, "hook_event_name": "PreToolUse"}, "codex")
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    assert run(project)["ok"]


def test_hook_rejects_changed_implementation_digest(project, cli, monkeypatch):
    def execute(command, prompt, env, timeout, cwd):
        result = cli.execute(command, prompt, env, timeout, cwd)
        if cli.calls[-1][5]["phase"] == "work":
            path = Path(env[claude.WORKER_ENV])
            data = json.loads(path.read_text(encoding="utf-8"))
            data["digest"] = "outdated"
            write_json(path, data)
            output = cli.hook(command, {"hook_event_name": "PreToolUse", "cwd": cwd,
                                       "tool_name": "Read", "tool_input": {"file_path": "notes.txt"}}, env, cwd)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        return result
    monkeypatch.setattr(claude, "run_process", execute)
    assert run(project)["ok"]


def test_project_guard_receives_owner_and_can_deny(project, cli, tmp_path):
    checker = tmp_path / "guard.py"
    checker.write_text("import json,sys\np=json.load(sys.stdin)\nassert p['session_id']=='orchard-owner'\nassert p['agent_id']=='chef-claude-worker'\nprint(json.dumps({'decision':'block','reason':'fictional policy'}))\n")
    # A denied probe still succeeds; the fixed project guard is present in the
    # immutable per-call context and is also applied to ordinary work tools.
    original = cli.execute
    def execute(command, prompt, env, timeout, cwd):
        result = original(command, prompt, env, timeout, cwd)
        if cli.calls[-1][5]["phase"] == "work":
            output = cli.hook(command, {"hook_event_name": "PreToolUse", "cwd": cwd,
                                       "tool_name": "Read", "tool_input": {"file_path": "notes.txt"}}, env, cwd)
            assert "fictional policy" in output["hookSpecificOutput"]["permissionDecisionReason"]
        return result
    from unittest.mock import patch
    with patch.object(claude, "run_process", execute):
        assert claude.run("dev-small", "Inspect", 240, str(project.root), project.state, project.active,
                          [sys.executable, str(checker)])["ok"]


@pytest.mark.parametrize("event", [{"type": "result", "subtype": "error_during_execution", "is_error": True},
                                   {"type": "assistant", "message": "partial"},
                                   {"type": "result", "subtype": "success", "is_error": True}])
def test_stream_error_or_missing_terminal_is_not_success(event):
    result = claude.parse_result(subprocess.CompletedProcess([], 0, json.dumps(event), ""), "researcher")
    assert not result["ok"] and result["error"]


def test_role_override_cannot_inject_cli_or_replace_instructions():
    assert claude.role_config("researcher", {"researcher": {"model": "fictional-model", "effort": "high"}})["model"] == "fictional-model"
    for override in ({"instructions": "other"}, {"effort": "invalid"}, {"model": "a\nb"}):
        with pytest.raises(ValueError):
            claude.role_config("researcher", {"researcher": override})


@pytest.mark.skipif(os.name == "nt", reason="Hermes CLI process groups target POSIX gateways")
def test_timeout_kills_descendant_that_ignores_term(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = "import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']);open(sys.argv[1],'w').write(str(p.pid));time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        claude.run_process([sys.executable, "-c", script, str(pidfile)], "", dict(os.environ), 0.5, str(tmp_path))
    pid = int(pidfile.read_text())
    # A killed child may briefly remain a zombie until the host reaps it.
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        status = Path(f"/proc/{pid}/stat")
        if status.exists() and status.read_text().split()[2] == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker descendant survived timeout")
