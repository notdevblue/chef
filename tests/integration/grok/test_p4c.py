import json
import subprocess
import sys
from pathlib import Path

from tests.paths import PLUGIN_ROOT

SCRIPT = (PLUGIN_ROOT / 'grok/skills/chef-mission/scripts/p4c.py')


def test_grok_p4_wrapper_uses_grok_adapter_and_runtime_client(tmp_path):
    project = tmp_path / "project"
    adapter_dir = project / ".grok" / "orchestrator"
    adapter_dir.mkdir(parents=True)
    adapter = {
        "schema": 1,
        "vars": {"main": str(project)},
        "slots": {"mainClient": "base-client"},
        "runtimes": {"grok": {"slots": {"mainClient": "grok-client"}}},
    }
    (adapter_dir / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project", str(project), "--dry-run", "edit", "example.txt"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "p4 -c grok-client edit example.txt"


def test_grok_p4_wrapper_rejects_client_from_another_runtime(tmp_path):
    project = tmp_path / "project"
    adapter_dir = project / ".grok" / "orchestrator"
    adapter_dir.mkdir(parents=True)
    adapter = {
        "schema": 1,
        "vars": {"main": str(project)},
        "slots": {"mainClient": "base-client"},
        "runtimes": {"grok": {"slots": {"mainClient": "grok-client"}}},
    }
    (adapter_dir / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project", str(project), "--client", "base-client", "--dry-run", "edit", "example.txt"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 2
    assert "grok-client" in result.stderr
    assert result.stdout == ""
