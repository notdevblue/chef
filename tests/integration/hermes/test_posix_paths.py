"""Native POSIX path spelling must not redirect project authority checks."""

import json
import os

import pytest

from chef.runtimes.hermes.plugin import Bridge
from chef.slots import commands
from chef.slots.storage import Rows


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal backslash directory")
def test_literal_backslash_foreign_project_is_checked_for_native_write(tmp_path):
    for name, owner in (("garden", "owner"), ("foreign\\garden", "other-owner")):
        project = tmp_path / name
        registration = project / ".hermes" / "orchestrator"
        registration.mkdir(parents=True)
        (registration / "adapter.json").write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"}}), encoding="utf-8")
        (project / "state").mkdir()
        (project / "state" / "ACTIVE").write_text(json.dumps({"id": "garden-mission", "session": owner, "runtime": "hermes"}), encoding="utf-8")
    bridge = Bridge()
    bridge.bindings[("owner", "task")] = {"cwd": str(tmp_path / "garden")}
    foreign = tmp_path / "foreign\\garden"
    result = bridge.before_tool("write_file", {"path": str(foreign / "source.py"), "content": "fictional"}, "owner", "task")
    assert result and result["action"] == "block"
    assert "another active mission session" in result["message"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal backslash directory")
def test_slot_metadata_preserves_native_backslash_root(tmp_path):
    project = tmp_path / "garden"
    registration = project / ".hermes" / "orchestrator"
    registration.mkdir(parents=True)
    root = tmp_path / "slots\\literal"
    body = {"schema": 1, "paths": {"stateDir": "state"}, "slots": {
        "enabled": True, "root": str(root), "client": "garden-{name}", "names": ["one"], "vcs": "none", "prepare": [],
    }}
    (registration / "adapter.json").write_text(json.dumps(body), encoding="utf-8")
    assert commands.main(["--runtime", "hermes", "--project", str(project), "create", "one"]) == 0
    assert Rows(str(project / "state" / "slots.tsv")).find("one")["path"] == str(root / "one")
