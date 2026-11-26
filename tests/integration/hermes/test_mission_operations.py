"""Mission authority is committed by a tool handler, after every pre-tool check."""

import json

import pytest

from chef.guards import mission
from chef.runtimes.hermes import plugin


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / "meadow"
    adapter = root / ".hermes/orchestrator/adapter.json"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"}}))
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(plugin, "native_cwd", lambda _: str(root))
    return root, plugin.Bridge()


def dispatch(bridge, args, owner="owner"):
    return json.loads(bridge.mission(args, session_id=owner, task_id="task"))


def test_prechecks_do_not_open_or_close_the_mission(managed):
    root, bridge = managed
    opening = {"action": "open", "mission": "seed"}
    assert bridge.before_tool("chef_mission", opening, "owner", "task") is None
    assert not (root / "state/ACTIVE").exists()
    assert dispatch(bridge, opening)["ok"]
    closing = {"action": "close", "mission": "seed"}
    assert bridge.before_tool("chef_mission", closing, "owner", "task") is None
    # A later plugin may deny the request. No handler means no state change.
    assert mission.read_active(str(root / "state")).id == "seed"
    assert dispatch(bridge, closing)["ok"]
    assert not (root / "state/ACTIVE").exists()


def test_handler_rechecks_ownership_and_status_is_read_only(managed):
    root, bridge = managed
    assert dispatch(bridge, {"action": "open", "mission": "seed"})["ok"]
    assert "error" in dispatch(bridge, {"action": "close", "mission": "seed"}, "other")
    assert dispatch(bridge, {"action": "status"})["owned"]
    assert not dispatch(bridge, {"action": "status"}, "other")["owned"]
    assert mission.read_active(str(root / "state")).session == "owner"


def test_shell_marker_cannot_mutate_state_before_later_guards(managed):
    root, bridge = managed
    result = bridge.before_tool("terminal", {"command": "echo han-mission open seed"}, "owner", "task")
    assert result["action"] == "block" and "chef_mission" in result["message"]
    assert not (root / "state/ACTIVE").exists()


def test_managed_close_detaches_tasks_and_restores_their_original_directory(managed, monkeypatch):
    root, bridge = managed
    restored = []
    monkeypatch.setattr(plugin, "bind_native_cwd", lambda task, cwd: restored.append((task, cwd)))
    bridge.bindings[("owner", "task")] = {"cwd": str(root), "return_cwd": str(root.parent)}
    assert dispatch(bridge, {"action": "open", "mission": "seed"})["ok"]
    assert dispatch(bridge, {"action": "close", "mission": "seed"})["ok"]
    assert restored == [("task", str(root.parent))]
    assert not bridge.bindings and "owner" not in bridge.released_slots


@pytest.mark.parametrize("args", [{"action": "close", "mission": "seed"},
    {"action": "open", "mission": "../seed"}, {"action": "open", "mission": "seed", "cwd": "/"}])
def test_missing_owner_or_untrusted_arguments_cannot_change_state(managed, args):
    root, bridge = managed
    assert "error" in dispatch(bridge, args)
    assert not (root / "state/ACTIVE").exists()
