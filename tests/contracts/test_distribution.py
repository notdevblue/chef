"""Execute the installed layout without the checkout or ambient Python paths."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.paths import PLUGIN_ROOT
from tools.check_architecture import inspect


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    root = tmp_path_factory.mktemp("distribution with spaces")
    target = root / "installed plugin"
    shutil.copytree(PLUGIN_ROOT, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return target, root


@pytest.mark.parametrize("relative", [
    "skills/mission/scripts/mission_state.py",
    "codex/skills/chef-mission/scripts/mission_state.py",
    "skills/slots/scripts/slots.py",
    "codex/skills/chef-slots/scripts/slots.py",
    "skills/slots/scripts/dashboard.py",
    "codex/skills/chef-slots/scripts/dashboard.py",
    "skills/doctor/scripts/doctor.py",
    "codex/skills/chef-doctor/scripts/doctor.py",
    "skills/distill/scripts/distill.py",
    "codex/skills/chef-distill/scripts/distill.py",
    "skills/mission/scripts/p4c.py",
    "codex/skills/chef-mission/scripts/p4c.py",
    "grok/skills/chef-mission/scripts/p4c.py",
    "skills/mission/scripts/check_human_doc.py",
    "skills/mission/scripts/run_metrics.py",
    "skills/mission/scripts/task_queue.py",
    "skills/mission/scripts/model_recommendations.py",
])
def test_installed_commands_resolve_without_checkout(installed, relative):
    target, cwd = installed
    result = subprocess.run([sys.executable, "-I", "-B", str(target / relative), "--help"],
                            cwd=cwd, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    if relative.endswith("p4c.py"):
        assert b"--client" in result.stdout and b"--dry-run" in result.stdout
    else:
        assert b"usage:" in result.stdout


@pytest.mark.parametrize("relative,runtime", [
    ("hooks/session_rules.py", "claude"),
    ("codex/session_rules.py", "codex"),
    ("hooks/session_rules.py", "grok"),
])
def test_installed_session_hooks_keep_transcript_contract(installed, relative, runtime):
    target, cwd = installed
    transcript = str(cwd / "fictional-session.jsonl")
    payload = {"hook_event_name": "SessionStart", "han_runtime": runtime,
               "cwd": str(cwd), "session_id": "fictional-session", "transcript_path": transcript}
    result = subprocess.run([sys.executable, "-I", "-B", str(target / relative)], cwd=cwd,
                            input=json.dumps(payload).encode(), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    if runtime in ("codex", "grok"):
        assert "--transcript" in result.stdout.decode("utf-8")
        assert "fictional-session.jsonl" in result.stdout.decode("utf-8")


def test_distribution_excludes_tests_and_enforces_dependencies():
    assert inspect() == []


def test_structure_check_rejects_deployment_tests_and_inverted_imports(tmp_path):
    package = tmp_path / "chef/infrastructure"
    package.mkdir(parents=True)
    (tmp_path / "test_accidental.py").write_text("", encoding="utf-8")
    (package / "bad.py").write_text("import sys\nsys.path.insert(0, 'scripts')\nimport chef.slots.commands\n", encoding="utf-8")
    errors = inspect(tmp_path)
    assert any("test in deployment" in error for error in errors)
    assert any("changes import paths" in error for error in errors)
    assert any("infrastructure imports" in error for error in errors)
