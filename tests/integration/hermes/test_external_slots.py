"""Native external-slot ownership without a real gateway or workspace service."""

import json
from pathlib import Path
import shlex
import sys
from contextvars import ContextVar
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from chef.guards import mission
from chef.infrastructure.errors import UsageError
from chef.infrastructure.locking import LockBusy
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
    marker = mission.MARKER.fullmatch(command)
    if marker and not extra:
        args = {"action": marker[1], "mission": marker[2]}
        denied = bridge.before_tool("chef_mission", args, session_id=session, task_id=session)
        if denied:
            return denied
        result = json.loads(bridge.mission(args, session_id=session, task_id=session))
        return {"action": "block", "message": result["error"]} if "error" in result else None
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


def test_close_restores_each_task_and_allows_only_declared_idle_reads(world, tmp_path, monkeypatch):
    manager, workspace, calls = world
    original = tmp_path / "original"
    original.mkdir()
    cwds = {"owner": str(manager), "second-task": str(original)}
    monkeypatch.setattr(plugin, "native_cwd", lambda task: cwds.get(task, str(manager)))
    monkeypatch.setattr(plugin, "bind_native_cwd", cwds.__setitem__)
    status = [sys.executable, str(manager / "inspect.py"), "status"]
    listing = [sys.executable, str(manager / "inspect.py"), "list"]
    adapter_path = tmp_path / "adapters" / "orchard" / "adapter.json"
    adapter = json.loads(adapter_path.read_text())
    adapter["runtimes"]["hermes"]["externalSlots"]["readOnlyCommands"] = [status, listing]
    adapter_path.write_text(json.dumps(adapter))
    bridge = plugin.Bridge()
    assert select(bridge, manager)["reservation"] == "UNLOCKED"
    assert terminal(bridge, "echo han-mission open garden") is None
    assert json.loads(bridge.slot({"project": str(manager), "name": "one", "resume": True},
                                  session_id="owner", task_id="second-task"))["reservation"] == "LOCKED"
    assert cwds == {"owner": str(workspace), "second-task": str(workspace)}
    assert terminal(bridge, "echo han-mission close garden") is None
    assert cwds == {"owner": str(manager), "second-task": str(original)}
    assert not bridge.bindings and bridge.request("terminal", {"command": "pwd"}, "owner", "owner") is None
    assert terminal(bridge, shlex.join(status), workdir=str(workspace)) is None
    assert terminal(bridge, shlex.join(listing), workdir=str(manager)) is None
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for workdir in (None, ".", "~/orchard"):
        assert terminal(bridge, shlex.join(listing), workdir=workdir)["action"] == "block"
    assert terminal(bridge, shlex.join(listing), workdir=str(manager), background=True)["action"] == "block"
    for suffix in ("; echo changed", " > note", " $(echo changed)", "\n echo changed"):
        assert terminal(bridge, shlex.join(status) + suffix, workdir=str(workspace))["action"] == "block"
    assert bridge.before_tool("write_file", {"path": str(workspace / "note"), "content": "x"}, "owner", "owner")["action"] == "block"

    # Another session can acquire the released slot and its guards still apply.
    assert select(bridge, manager, session="other")["external"]
    assert terminal(bridge, "echo han-mission open second", session="other") is None
    denied = terminal(bridge, shlex.join(status), workdir=str(workspace))
    assert "another active mission" in denied["message"]
    assert terminal(bridge, "echo han-mission close second", workdir=str(workspace))["action"] == "block"
    assert mission.read_active(str(workspace / "state")).session == "other"
    assert [action for action, _ in calls].count("release") == 1


def test_detached_reads_cannot_override_guard_rules_or_an_unresolved_journal(world, tmp_path):
    manager, workspace, _ = world
    argv = [sys.executable, str(manager / "inspect.py"), "status"]
    command = shlex.join(argv)
    adapter_path = tmp_path / "adapters" / "orchard" / "adapter.json"
    adapter = json.loads(adapter_path.read_text())
    settings = adapter["runtimes"]["hermes"]["externalSlots"]
    settings["readOnlyCommands"] = [argv]
    settings["protectedCommands"] = ["status"]
    adapter_path.write_text(json.dumps(adapter))
    bridge = plugin.Bridge()
    assert select(bridge, manager)["external"]
    assert terminal(bridge, "echo han-mission open seed") is None
    assert terminal(bridge, "echo han-mission close seed") is None
    assert "belong to the mission guard" in terminal(bridge, command, workdir=str(workspace))["message"]
    settings["protectedCommands"] = []
    adapter["guard"] = {"deny": [{"pattern": "status", "reason": "fictional deny rule"}]}
    adapter_path.write_text(json.dumps(adapter))
    assert "fictional deny rule" in terminal(bridge, command, workdir=str(workspace))["message"]
    adapter["guard"] = {}
    adapter_path.write_text(json.dumps(adapter))
    journal, _ = external_slots.reservation_paths(str(workspace.resolve()))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}")
    assert "unresolved reservation" in terminal(bridge, command, workdir=str(workspace))["message"]


def test_detached_slot_read_checks_provider_when_another_profile_has_a_different_state_dir(world, tmp_path, monkeypatch):
    manager, workspace, _ = world
    argv = [sys.executable, str(manager / "inspect.py"), "status"]
    default_registry = tmp_path / "adapters"
    originally = json.loads((default_registry / "orchard" / "adapter.json").read_text())
    originally["runtimes"]["hermes"]["externalSlots"]["readOnlyCommands"] = [argv]
    homes = [tmp_path / "home" / "profiles" / name for name in ("fern", "spruce")]
    for home in homes:
        registry = home / "chef" / "adapters"
        (registry / "orchard").mkdir(parents=True)
        (registry / "projects.json").write_bytes((default_registry / "projects.json").read_bytes())
        (registry / "orchard" / "adapter.json").write_text(json.dumps({
            **originally, "paths": {"stateDir": home.name + "-state"},
        }))
    monkeypatch.delenv("HAN_ADAPTERS_DIR")
    current_home = ContextVar("hermes_home", default=homes[0])
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=current_home.get))
    owner = []

    def provider(slot, action, record=None, clean=False):
        if action == "status" and owner:
            raise UsageError("provider reports the workspace is occupied")
        if action == "acquire":
            assert not owner
            owner.append(record["who"])
        if action in ("check", "release"):
            assert owner == [record["who"]]
        if action == "release":
            owner.clear()

    monkeypatch.setattr(external_slots.Slot, "call", provider)
    first, second = plugin.Bridge(), plugin.Bridge()
    assert select(first, manager)["external"]
    assert terminal(first, "echo han-mission open seed") is None
    assert terminal(first, "echo han-mission close seed") is None
    assert terminal(first, shlex.join(argv), workdir=str(workspace)) is None
    token = current_home.set(homes[1])
    try:
        assert select(second, manager, session="other")["external"]
        assert terminal(second, "echo han-mission open tree", session="other") is None
    finally:
        current_home.reset(token)
    assert not (workspace / "fern-state" / "ACTIVE").exists()
    assert "provider reports the workspace is occupied" in terminal(
        first, shlex.join(argv), workdir=str(workspace)
    )["message"]
    assert owner == ["other"]


def test_close_blocks_concurrent_resume_binding(world, monkeypatch):
    manager, _, _ = world
    bridge = plugin.Bridge()
    assert select(bridge, manager)["external"]
    assert terminal(bridge, "echo han-mission open garden") is None
    checking, proceed = Event(), Event()
    outcomes = []
    closing = Thread(target=lambda: outcomes.append(terminal(bridge, "echo han-mission close garden")))

    def provider(slot, action, record=None, clean=False):
        if action == "check" and current_thread() is closing:
            checking.set()
            assert proceed.wait(10)

    monkeypatch.setattr(external_slots.Slot, "call", provider)
    closing.start()
    try:
        assert checking.wait(10)
        for task in ("owner", "other-task"):
            result = json.loads(bridge.slot({"project": str(manager), "name": "one", "resume": True},
                                            session_id="owner", task_id=task))
            assert "transition is in progress" in result.get("error", ""), result
    finally:
        proceed.set()
        closing.join(10)
    assert outcomes == [None]
    assert not bridge.bindings


def test_selection_records_the_native_cwd_observed_under_the_binding_lock(world, monkeypatch):
    manager, workspace, _ = world
    # A previous close can restore the cwd between the backend probe and the bind.
    observed = iter((str(workspace), str(manager)))
    monkeypatch.setattr(plugin, "native_cwd", lambda _: next(observed, str(manager)))
    changes = []
    monkeypatch.setattr(plugin, "bind_native_cwd", lambda task, cwd: changes.append(cwd))
    bridge = plugin.Bridge()
    assert select(bridge, manager)["external"]
    assert terminal(bridge, "echo han-mission open garden") is None
    assert terminal(bridge, "echo han-mission close garden") is None
    assert changes == [str(workspace), str(manager)]


def test_native_restore_error_after_release_still_detaches_other_tasks(world, monkeypatch):
    manager, workspace, calls = world
    cwds = {}
    monkeypatch.setattr(plugin, "native_cwd", lambda task: cwds.get(task, str(manager)))

    def bind(task, cwd):
        if task == "owner" and cwd == str(manager):
            raise ValueError("fictional native failure")
        cwds[task] = cwd

    monkeypatch.setattr(plugin, "bind_native_cwd", bind)
    bridge = plugin.Bridge()
    assert select(bridge, manager)["external"]
    assert terminal(bridge, "echo han-mission open garden") is None
    assert json.loads(bridge.slot({"project": str(manager), "name": "one", "resume": True},
                                  session_id="owner", task_id="second-task"))["external"]
    assert "was released" in terminal(bridge, "echo han-mission close garden")["message"]
    assert cwds["second-task"] == str(manager) and not bridge.bindings
    assert not mission.read_active(str(workspace / "state")).id
    assert calls[-1] == ("release", "original-request")


def test_existing_gateway_journal_keeps_its_lock_until_release(world, tmp_path, monkeypatch):
    manager, workspace, calls = world
    previous = plugin.Bridge()
    context = select(previous, manager)
    legacy = external_slots.from_context(context).journal
    assert terminal(previous, "echo han-mission open garden") is None

    profile = tmp_path / "home" / "profiles" / "fern"
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: profile))
    bridge = plugin.Bridge()
    assert "error" in select(bridge, manager, session="other", resume=True)
    assert select(bridge, manager, resume=True)["mission"] == "garden"
    assert external_slots.from_context(context).journal == legacy
    with monkeypatch.context() as child:
        child.delitem(sys.modules, "hermes_constants")
        child.setenv("HERMES_HOME", str(profile))
        assert not workers.worker_denial({
            "cwd": str(workspace), "tool_name": "Bash", "tool_input": {"command": "git status"},
            "han_hermes_worker": {"external": context, "gatewayHome": str(tmp_path / "home"),
                                  "state": context["state"], "cwd": str(workspace),
                                  "session": "owner", "mission": "garden"},
        })
    assert terminal(bridge, "echo han-mission close garden") is None
    assert calls[-1] == ("release", "original-request")
    assert not legacy.exists() and not mission.read_active(str(workspace / "state")).id
    assert external_slots.from_context(context).journal.parent == profile / "chef" / "external-slots"


def test_gateway_and_profile_journals_require_inspection(world, tmp_path, monkeypatch):
    manager, workspace, calls = world
    bridge = plugin.Bridge()
    context = select(bridge, manager)
    old_journal = external_slots.from_context(context).journal
    assert terminal(bridge, "echo han-mission open garden") is None
    profile = tmp_path / "home" / "profiles" / "fern"
    new_journal = profile / "chef" / "external-slots" / old_journal.name
    new_journal.parent.mkdir(parents=True)
    snapshot = old_journal.read_bytes()
    new_journal.write_bytes(snapshot)
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: profile))
    assert "both Hermes homes" in terminal(bridge, "echo han-mission close garden")["message"]
    assert old_journal.read_bytes() == new_journal.read_bytes() == snapshot
    assert mission.read_active(str(workspace / "state")).id == "garden"
    assert all(action != "release" for action, _ in calls)


@pytest.mark.parametrize("state_dir", ["state", "other-state"])
def test_two_homes_cannot_open_the_same_workspace_concurrently(world, tmp_path, monkeypatch, state_dir):
    manager, workspace, calls = world
    gateway = tmp_path / "home"
    profile = gateway / "profiles" / "fern"
    original = tmp_path / "adapters"
    for home in (gateway, profile):
        registry = home / "chef" / "adapters"
        (registry / "orchard").mkdir(parents=True)
        for filename in ("orchard/adapter.json", "projects.json"):
            (registry / filename).write_bytes((original / filename).read_bytes())
    profile_adapter = profile / "chef" / "adapters" / "orchard" / "adapter.json"
    data = json.loads(profile_adapter.read_text())
    data["paths"]["stateDir"] = state_dir
    profile_adapter.write_text(json.dumps(data))
    monkeypatch.delenv("HAN_ADAPTERS_DIR")
    current_home = ContextVar("hermes_home", default=gateway)
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=current_home.get))
    default_bridge, profile_bridge = plugin.Bridge(), plugin.Bridge()
    assert select(default_bridge, manager)["external"]
    token = current_home.set(profile)
    assert select(profile_bridge, manager, session="other")["external"]
    current_home.reset(token)
    inspecting, proceed = Event(), Event()
    outcome = []

    def provider(slot, action, record=None, clean=False):
        calls.append((action, (record or {}).get("key")))
        if action == "status" and current_home.get() == gateway:
            inspecting.set()
            assert proceed.wait(10)

    monkeypatch.setattr(external_slots.Slot, "call", provider)
    first = Thread(target=lambda: outcome.append(terminal(default_bridge, "echo han-mission open garden")))
    first.start()
    try:
        assert inspecting.wait(10)
        token = current_home.set(profile)
        try:
            second = terminal(profile_bridge, "echo han-mission open garden", session="other")
        finally:
            current_home.reset(token)
        assert second and "transition is in progress" in second["message"]
        assert not list((profile / "chef" / "external-slots").glob("*.json"))
    finally:
        proceed.set()
        first.join(10)
    assert outcome == [None]
    assert sum(action == "acquire" for action, _ in calls) == 1
    assert mission.read_active(str(workspace / "state")).session == "owner"
    assert terminal(default_bridge, "echo han-mission close garden") is None


def test_workspace_transition_uses_filesystem_identity(tmp_path):
    folder = tmp_path / "workspace"
    folder.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(folder, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are unavailable")
    with external_slots.workspace_transition(str(folder)):
        with pytest.raises(LockBusy, match="transition is in progress"):
            with external_slots.workspace_transition(str(alias)):
                pytest.fail("both paths refer to the same workspace")


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
