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


@pytest.mark.skipif(os.name == "nt", reason="POSIX python3-only installation")
def test_installed_hook_starts_with_only_python3_on_original_path(tmp_path):
    original_bin = tmp_path / "only python3"
    original_bin.mkdir()
    (original_bin / "python3").symlink_to(sys.executable)
    original = {**os.environ, "PATH": str(original_bin)}
    assert shutil.which("python", path=original["PATH"]) is None
    env = python_environment(original, cache_root=tmp_path / "private cache")
    plugin = tmp_path / "plugin with spaces"
    shutil.copytree(PLUGIN_ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    payload = {"hook_event_name": "SessionStart", "session_id": "fictional-session",
               "cwd": str(tmp_path), "transcript_path": "fictional-session.jsonl"}
    result = subprocess.run([shutil.which("python", path=env["PATH"]), str(plugin / "codex/session_rules.py")],
                            env=env, input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "fictional-session.jsonl" in result.stdout
    assert original["PATH"] == str(original_bin)


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
