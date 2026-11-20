"""Real child hook startup on hosts with python3 but no system python alias."""

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
def test_installed_hook_uses_the_only_interpreter_on_path(tmp_path, interpreter):
    original_bin = tmp_path / f"only {interpreter}"
    original_bin.mkdir()
    (original_bin / interpreter).symlink_to(sys.executable)
    original = {**os.environ, "PATH": str(original_bin)}
    env = python_environment(original, cache_root=tmp_path / "private cache")
    plugin = tmp_path / "plugin with spaces"
    shutil.copytree(PLUGIN_ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    payload = {"hook_event_name": "SessionStart", "session_id": "fictional-session",
               "cwd": str(tmp_path), "transcript_path": "fictional-session.jsonl"}
    codex_hook = json.loads((plugin / "codex/hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
    shared_hook = json.loads((plugin / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]
    shared_argv = [shared_hook["command"], *[arg.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)) for arg in shared_hook["args"]]]
    codex_command = codex_hook["command"].replace("${PLUGIN_ROOT}", str(plugin))
    claude_env_file = tmp_path / "claude.env"
    for command, child_env, hook_payload in (
        (codex_command, original, payload),
        (shared_argv, {**env, "CLAUDE_ENV_FILE": str(claude_env_file)}, payload),
        (shared_argv, env, {**payload, "han_runtime": "grok"}),
    ):
        result = subprocess.run(command, shell=isinstance(command, str), env=child_env, input=json.dumps(hook_payload),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        if "CLAUDE_ENV_FILE" in child_env:
            assert 'export HAN_TRANSCRIPT="fictional-session.jsonl"' in claude_env_file.read_text(encoding="utf-8")
        else:
            assert "fictional-session.jsonl" in result.stdout
    assert shutil.which("python3", path=env["PATH"])
    assert shutil.which("python", path=env["PATH"])
    assert original["PATH"] == str(original_bin)


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
