"""Native external-slot ownership without a real gateway or workspace service."""

import json
from pathlib import Path
import sys

import pytest

from chef.guards import mission
from chef.infrastructure.errors import UsageError
from chef.runtimes.hermes import external_slots, plugin, workers


@pytest.fixture
def world(tmp_path, monkeypatch):
    registry = tmp_path / "adapters"
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(registry))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    manager = tmp_path / "orchard"
    workspace = tmp_path / "orchard.one"
    manager.mkdir()
    workspace.mkdir()
    adapter_path = registry / "orchard" / "adapter.json"
    adapter_path.parent.mkdir(parents=True)
    adapter_path.write_text(json.dumps({
        "schema": 1, "paths": {"stateDir": "state"}, "agents": {"allow": ["chef:researcher"]},
        "runtimes": {"hermes": {"externalSlots": {
            "root": str(tmp_path), "provider": [sys.executable],
            "protectedCommands": ["reserve (?:lock|unlock)"],
            "workspaces": {"one": {"path": str(workspace)}}}}},
    }))
    (registry / "projects.json").write_text(json.dumps([{
        "adapter": "orchard", "roots": [str(manager), str(workspace)]}]))
    monkeypatch.setattr(plugin, "native_cwd", lambda _: str(manager))
    monkeypatch.setattr(plugin, "bind_native_cwd", lambda *_: None)
    monkeypatch.setattr(plugin, "originating_request", lambda _: "original-request")
    calls = []

    def provider(self, action, record=None, clean=False):
        calls.append((action, (record or {}).get("key")))

    monkeypatch.setattr(external_slots.Slot, "call", provider)
    return manager, workspace, calls


def select(bridge, manager, *, name="one", session="owner", dry_run=False, resume=False):
    return json.loads(bridge.slot({"project": str(manager), "name": name, "dry_run": dry_run, "resume": resume},
                                  session_id=session, task_id=session))


def terminal(bridge, command, session="owner", **extra):
    return bridge.before_tool("terminal", {"command": command, **extra}, session_id=session, task_id=session)


def test_select_is_read_only_and_close_uses_original_request(world, monkeypatch):
    manager, workspace, calls = world
    bridge = plugin.Bridge()
    assert Path(select(bridge, manager, dry_run=True)["cwd"]) == workspace
    context = select(bridge, manager)
    slot = external_slots.from_context(context)
    assert not slot.journal.exists() and set(action for action, _ in calls) == {"status"}
    assert terminal(bridge, "git status")["action"] == "block"
    assert terminal(bridge, "echo han-mission open garden && git status")["action"] == "block"
    assert terminal(bridge, "echo han-mission open garden") is None
    assert mission.read_active(slot.state).id == "garden"
    assert terminal(bridge, "git status") is None
    assert "belong to the mission guard" in terminal(bridge, "reserve unlock")["message"]
    assert terminal(bridge, "git status", workdir=str(manager))["action"] == "block"
    assert "error" in select(plugin.Bridge(), manager, session="other", resume=True)
    assert select(bridge, manager, resume=True)["mission"] == "garden"
    monkeypatch.setattr(plugin, "originating_request", lambda _: "next-request")
    assert terminal(bridge, "echo han-mission close garden") is None
    assert calls[-1] == ("release", "original-request")
    assert not slot.journal.exists() and not mission.read_active(slot.state).id


def test_release_failure_blocks_writes_and_new_admission(world, monkeypatch):
    manager, _, calls = world
    bridge = plugin.Bridge()
    slot = external_slots.from_context(select(bridge, manager))
    assert terminal(bridge, "echo han-mission open garden") is None

    def provider(self, action, record=None, clean=False):
        if action == "release":
            raise UsageError("external release failed")

    monkeypatch.setattr(external_slots.Slot, "call", provider)
    assert "release failed" in terminal(bridge, "echo han-mission close garden")["message"]
    assert slot.record()["phase"] == "closing"
    assert mission.read_active(slot.state).id == "garden"
    assert terminal(bridge, "git status")["action"] == "block"
    assert "error" in select(plugin.Bridge(), manager, session="other")
    assert ("acquire", "original-request") in calls


def test_unknown_acquisition_retains_evidence_but_does_not_open(world, monkeypatch):
    manager, _, _ = world
    bridge = plugin.Bridge()
    slot = external_slots.from_context(select(bridge, manager))

    def provider(self, action, record=None, clean=False):
        if action == "acquire":
            raise UsageError("external acquire timed out")

    monkeypatch.setattr(external_slots.Slot, "call", provider)
    assert terminal(bridge, "echo han-mission open garden")["action"] == "block"
    assert slot.record()["phase"] == "opening"
    assert not mission.read_active(slot.state).id
    assert "error" in select(bridge, manager)


def test_transition_and_delegate_are_serialized(world, monkeypatch):
    manager, _, _ = world
    bridge = plugin.Bridge()
    select(bridge, manager)
    assert terminal(bridge, "echo han-mission open garden") is None

    def worker(args, **kwargs):
        assert kwargs["external"]["slot"] == "one"
        assert terminal(bridge, "echo han-mission close garden")["action"] == "block"
        return {"ok": True}

    monkeypatch.setattr(workers, "delegate", worker)
    assert json.loads(bridge.delegate({"role": "researcher", "prompt": "read"}, "owner", "owner"))["ok"]
    assert terminal(bridge, "echo han-mission close garden") is None


def test_unbound_markers_and_undeclared_names_are_blocked(world):
    manager, workspace, _ = world
    assert "error" in select(plugin.Bridge(), manager, name="orchard.one")
    assert terminal(plugin.Bridge(), "echo han-mission open garden", workdir=str(workspace))["action"] == "block"
    outcome = mission.decide({"han_runtime": "hermes", "cwd": str(workspace), "session_id": "owner",
                              "tool_name": "Bash", "tool_input": {"command": "echo han-mission open garden"}})
    assert outcome.deny
    assert not (workspace / "state" / "ACTIVE").exists()
