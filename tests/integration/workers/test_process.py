import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.workers import process


def test_stdin_and_separate_streams_are_retained(tmp_path):
    result = process.run([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err',file=sys.stderr)"],
                         'quoted " text\nsecond line', cwd=tmp_path, env=dict(os.environ), timeout=10)
    assert result.returncode == 0 and result.stdout.splitlines() == ['quoted " text', 'second line']
    assert result.stderr.strip() == "err"


def test_cancel_terminates_owned_process(tmp_path):
    with pytest.raises(process.Cancelled):
        process.run([sys.executable, "-c", "import time; time.sleep(30)"], "", cwd=tmp_path,
                    env=dict(os.environ), timeout=10, cancelled=lambda: True)


def test_timeout_terminates_owned_process(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        process.run([sys.executable, "-c", "import time; time.sleep(30)"], "", cwd=tmp_path,
                    env=dict(os.environ), timeout=0.1)


def test_output_overflow_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "MAX_OUTPUT", 8)
    with pytest.raises(RuntimeError, match="size limit"):
        process.run([sys.executable, "-c", "print('x'*100)"], "", cwd=tmp_path,
                    env=dict(os.environ), timeout=10)


def test_global_npm_shim_uses_path_node_without_shell_expansion(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    npm.mkdir()
    shim = npm / "codex.cmd"
    shim.write_text("fixture")
    script = npm / "node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("fixture")
    node = tmp_path / "Program Files/nodejs/node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"fixture")
    monkeypatch.setattr(process, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process.shutil, "which", lambda name: str(node) if name == "node" else None)
    argv = [str(shim), "exec", "--instructions", 'literal %SECRET% & " text']
    assert process.literal_command(argv) == [str(node), str(script), *argv[1:]]


def test_global_npm_missing_native_node_fails_before_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="directly executable"):
        process.literal_command([str(tmp_path / "codex.cmd"), "exec"])
