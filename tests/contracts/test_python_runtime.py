"""Run installed hook commands with isolated interpreter paths."""

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chef.infrastructure.python import python_environment
from tests.paths import PLUGIN_ROOT


def test_unsupported_python_is_rejected_before_loading_a_product_module(monkeypatch):
    from chef.cli import entrypoints
    monkeypatch.setattr(entrypoints.sys, "version_info", (3, 9))
    with pytest.raises(SystemExit, match="Python 3.11 or newer"):
        entrypoints.expose({"__name__": "fictional_module"}, "must_not_import_missing_module")


@pytest.mark.skipif(os.name == "nt", reason="POSIX interpreter aliases")
@pytest.mark.parametrize("interpreter", ["python3", "python"])
def test_installed_codex_hook_uses_the_only_interpreter_on_path(tmp_path, interpreter):
    original_bin = tmp_path / f"only {interpreter}"
    original_bin.mkdir()
    (original_bin / interpreter).symlink_to(sys.executable)
    plugin = tmp_path / "plugin with spaces"
    shutil.copytree(PLUGIN_ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    payload = {"hook_event_name": "SessionStart", "session_id": "fictional-session",
               "cwd": str(tmp_path), "transcript_path": "fictional-session.jsonl"}
    codex_hook = json.loads((plugin / "codex/hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
    codex_command = codex_hook["command"].replace("${PLUGIN_ROOT}", str(plugin))
    result = subprocess.run(["/bin/sh", "-c", codex_command], env={**os.environ, "PATH": str(original_bin)},
                            input=json.dumps(payload).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert b"fictional-session.jsonl" in result.stdout


@pytest.fixture(params=[
    pytest.param("python3", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX interpreter alias")),
    pytest.param("python", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX interpreter alias")),
    *[pytest.param(shell, marks=pytest.mark.skipif(os.name != "nt", reason="Windows hook shell"))
      for shell in ("powershell", "pwsh", "git-bash", "cmd")],
])
def shared_hook_shell(request, tmp_path):
    name = request.param
    if name in ("python3", "python"):
        bin_dir = tmp_path / "only interpreter"
        bin_dir.mkdir()
        (bin_dir / name).symlink_to(sys.executable)
        return ["/bin/sh", "-c"], str(bin_dir)

    if name == "git-bash":
        git = shutil.which("git.exe")
        candidates = [Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe"]
        if git:
            candidates.insert(0, Path(git).parent.parent / "bin/bash.exe")
        executable = next((str(path) for path in candidates if path.is_file()), None)
    else:
        executable = shutil.which(f"{name}.exe")
    if not executable:
        pytest.skip(f"{name} is unavailable")
    prefix = {
        "powershell": ["-NoProfile", "-NonInteractive", "-Command"],
        "pwsh": ["-NoProfile", "-NonInteractive", "-Command"],
        "git-bash": ["-c"],
        "cmd": ["/C"],
    }[name]
    return [executable, *prefix], str(Path(sys.executable).parent)


@pytest.mark.parametrize("runtime", ["claude", "grok"])
def test_installed_shared_hook_preserves_session_and_guard_protocol(tmp_path, shared_hook_shell, runtime):
    shell, bin_dir = shared_hook_shell
    plugin = tmp_path / "plugin with spaces $and 'quotes'"
    shutil.copytree(PLUGIN_ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    hooks = json.loads((plugin / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    env = {**os.environ, "PATH": bin_dir, "CLAUDE_PLUGIN_ROOT": str(plugin),
           "HAN_ADAPTERS_DIR": str(tmp_path / "empty registry")}
    for key in ("GROK_HOOK_EVENT", "GROK_SESSION_ID", "CLAUDE_ENV_FILE", "CHEF_HERMES_WORKER", "HAN_REPO"):
        env.pop(key, None)

    def run(event, payload):
        handler = hooks[event][0]["hooks"][0]
        assert handler["type"] == "command" and "args" not in handler
        command = handler["command"]
        return subprocess.run([*shell, command], env=env, input=json.dumps(payload).encode(), capture_output=True,
                              cwd=tmp_path, timeout=30)

    session = "fictional-session"
    transcript = "fictional-session.jsonl"
    if runtime == "claude":
        env_file = tmp_path / "claude.env"
        env["CLAUDE_ENV_FILE"] = str(env_file)
        started = {"hook_event_name": "SessionStart", "session_id": session, "transcript_path": transcript}
        tool = {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": "Bash",
                "tool_input": {"command": "release-tool publish", "shell": "bash"}}
    else:
        started = {"hookEventName": "session_start", "sessionId": session, "transcriptPath": transcript}
        tool = {"hookEventName": "pre_tool_use", "sessionId": session, "toolName": "run_terminal_command",
                "toolInput": {"command": "release-tool publish", "shell": "bash"}}
    result = run("SessionStart", {**started, "cwd": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    if runtime == "claude":
        assert f'export HAN_TRANSCRIPT="{transcript}"' in env_file.read_text(encoding="utf-8")
    else:
        assert transcript in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    adapter = tmp_path / f".{runtime}/orchestrator/adapter.json"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"},
                                  "guard": {"deny": [{"pattern": "^release-tool publish$", "reason": "release blocked"}]}}),
                       encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "ACTIVE").write_text(json.dumps({"id": "garden", "session": session}), encoding="utf-8")
    tool["cwd"] = str(tmp_path)
    result = run("PreToolUse", tool)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "release blocked" in decision["permissionDecisionReason"]

    # Chef's guard uses a JSON denial. Exercise the host's other denial protocol
    # through the same configured command, including byte-for-byte stdio.
    (plugin / "hooks/mission_guard.py").write_text(
        "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        "sys.stderr.buffer.write(b'fixture denial')\nsys.exit(2)\n", encoding="utf-8",
    )
    denied = run("PreToolUse", tool)
    assert denied.returncode == 2
    assert denied.stdout == json.dumps(tool).encode()
    assert denied.stderr == b"fixture denial"


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook launcher")
def test_posix_hook_launcher_returns_the_denial_without_retrying(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    fallback = bin_dir / "python"
    fallback.write_text("#!/bin/sh\necho unexpected fallback >&2\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o700)
    script = tmp_path / "hook with spaces.py"
    script.write_text("import sys\nassert sys.stdin.read() == 'payload'\nsys.exit(2)\n", encoding="utf-8")
    result = subprocess.run(
        ["/bin/sh", str(PLUGIN_ROOT / "hooks/run-python.sh"), str(script)],
        env={**os.environ, "PATH": str(bin_dir)}, input="payload", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 2
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX alias")
def test_python_alias_is_stable_and_does_not_overwrite_an_existing_file(tmp_path):
    first = python_environment({"PATH": "/fictional"}, cache_root=tmp_path)
    assert first == python_environment({"PATH": "/fictional"}, cache_root=tmp_path)
    alias = Path(first["PATH"].split(os.pathsep)[0]) / "python"
    alias.unlink()
    alias.write_text("user file", encoding="utf-8")
    with pytest.raises(ValueError, match="another target"):
        python_environment({"PATH": "/fictional"}, cache_root=tmp_path)
    assert alias.read_text() == "user file"


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_python_launcher_preserves_virtual_environment_prefix(tmp_path):
    env = python_environment(cache_root=tmp_path)
    result = subprocess.check_output([shutil.which("python", path=env["PATH"]), "-c", "import sys; print(sys.prefix)"],
                                     env=env, text=True, timeout=15)
    assert result.strip() == sys.prefix


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_concurrent_python_launcher_publication_is_complete(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        environments = list(pool.map(lambda _: python_environment({"PATH": "/fixture"}, cache_root=tmp_path), range(16)))
    assert all(env == environments[0] for env in environments)
    assert not list(tmp_path.rglob(".python-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX cache node types and permissions")
def test_python_launcher_rejects_special_files_and_writable_cache(tmp_path):
    env = python_environment(cache_root=tmp_path)
    directory = Path(env["PATH"].split(os.pathsep)[0])
    alias = directory / "python"
    alias.unlink()
    os.mkfifo(alias)
    with pytest.raises(ValueError, match="another target"):
        python_environment(cache_root=tmp_path)
    alias.unlink()
    directory.chmod(0o777)
    with pytest.raises(ValueError, match="owned directory"):
        python_environment(cache_root=tmp_path)


def test_runtime_entrypoint_is_available_from_an_installed_tree(tmp_path):
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN_ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run([sys.executable, "-I", str(plugin / "runtime.py"), "--help"],
                            capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "hermes" in result.stdout and "codex" in result.stdout
