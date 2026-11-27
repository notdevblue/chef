"""Independent projects, literal search data and bounded worker capabilities."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chef.guards import commands, mission
from chef.infrastructure.fingerprints import execution_digest, implementation_digest
from chef.verification import execution
from chef.workers import context


@pytest.mark.parametrize("command", ["git status --short", "git diff --stat", "rg --glob '*.py' submit .",
    "rg 'alpha|beta' notes.txt", "Get-Content notes.txt | Select-Object -First 5", "certutil -hashfile notes.txt SHA256"])
def test_read_operations_are_identified(command):
    assert commands.readonly(command, "PowerShell")


@pytest.mark.parametrize("command", ["git reset --hard", "git diff --output=changed.txt", "rg --pre=writer pattern .",
    "rg pattern . > changed.txt", "cat notes.txt | sh", "echo $(writer)", "python -c 'open(\"x\",\"w\")'",
    "Get-Content notes.txt; Remove-Item notes.txt"])
def test_reads_cannot_grant_write_or_execution_permissions(command):
    assert not commands.readonly(command, "PowerShell")


def test_searching_control_names_is_not_a_control_invocation():
    assert not commands.control_invocation("rg -n mission_state.py notes.txt")
    assert commands.control_invocation('python "tools/mission_state.py" --runtime codex init')


def test_scanning_one_workspace_does_not_block_another(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    roots = [tmp_path / "alpha", tmp_path / "beta"]
    for root in roots:
        root.mkdir()
        (root / ".git").mkdir()
        (root / "input.txt").write_text("input", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original = execution.input_file_aliases

    def scan(targets):
        if str(roots[0]).casefold() in str(targets[0]).casefold():
            entered.set()
            assert release.wait(5)
        return original(targets)

    monkeypatch.setattr(execution, "input_file_aliases", scan)

    def first():
        with execution.workspace_scope(roots[0]), execution.input_lease([roots[0] / "input.txt"]):
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        assert entered.wait(5)
        try:
            with execution.workspace_scope(roots[1]), execution.input_lease([roots[1] / "input.txt"]):
                with pytest.raises(execution.VerificationError, match="protected"):
                    execution.require_mutation_allowed([roots[1] / "input.txt"])
        finally:
            release.set()
        future.result(timeout=5)


def test_read_does_not_consult_a_corrupt_execution_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "registry_directory", lambda: tmp_path)
    (tmp_path / "inputs.json").write_text("{bad", encoding="utf-8")
    assert not mission.execution_write_reason({"tool_name": "Bash", "cwd": str(tmp_path),
                                               "tool_input": {"command": "rg --files"}})


def test_another_process_holding_one_workspace_gate_does_not_block_a_second(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    first, second = tmp_path / "alpha", tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    source = str(Path(execution.__file__).resolve().parents[2])
    script = """import sys
from chef.verification import execution
with execution.workspace_scope(sys.argv[1]):
    with execution.locking.FileLock(execution.registry_directory() / 'inputs.lock', 'fixture'):
        print('READY', flush=True)
        sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(first)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "PYTHONPATH": source})
    try:
        assert process.stdout.readline().strip() == "READY"
        with execution.workspace_scope(second), execution.input_lease([second / "input.txt"]):
            pass
    finally:
        process.communicate("release\n", timeout=10)
    assert process.returncode == 0


def test_dashboard_changes_do_not_revoke_a_worker_but_guard_changes_do(tmp_path):
    (tmp_path / "dashboard").mkdir()
    ui = tmp_path / "dashboard/view.py"
    policy = tmp_path / "guard.py"
    ui.write_text("theme = 1", encoding="utf-8")
    policy.write_text("protected = True", encoding="utf-8")
    before, complete = execution_digest(tmp_path), implementation_digest(tmp_path)
    ui.write_text("theme = 2", encoding="utf-8")
    assert execution_digest(tmp_path) == before
    assert implementation_digest(tmp_path) != complete
    policy.write_text("protected = False", encoding="utf-8")
    assert execution_digest(tmp_path) != before


def test_blocked_worker_retains_a_report_before_it_is_stopped(tmp_path):
    data = {"phase": "work", "contextPath": str(tmp_path / "context.json")}
    payload = {"han_worker": data, "tool_input": {"command": "a denied operation"}}
    context.acknowledge(payload, "outside assigned files")
    context.acknowledge(payload, "outside assigned files")
    result = json.loads((tmp_path / "blocked.json").read_text())
    assert result == {"count": 2, "command": "a denied operation", "reason": "outside assigned files"}
