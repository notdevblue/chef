"""Current-session entry does not depend on a desktop or claim another mission."""

import json
from pathlib import Path

import pytest

from chef.guards import mission as guard
from chef.infrastructure.errors import UsageError
from chef.slots import commands as slots


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty"))
    project = tmp_path / "orchard"
    project.mkdir()
    folder = project / ".hermes/orchestrator"
    folder.mkdir(parents=True)
    adapter = {"schema": 1, "paths": {"stateDir": "state"}, "slots": {
        "enabled": True, "vcs": "none", "root": str(tmp_path / "slots"),
        "client": "test-{name}", "names": ["one"], "prepare": []}}
    (folder / "adapter.json").write_text(json.dumps(adapter))
    assert slots.main(["--project", str(project), "--runtime", "hermes", "create", "one"]) == 0
    slot_adapter = tmp_path / "slots/one/.hermes/orchestrator"
    slot_adapter.mkdir(parents=True)
    (slot_adapter / "adapter.json").write_text(json.dumps(adapter))
    return project, tmp_path / "slots/one"


def test_entry_uses_slot_without_spawning_or_claiming_active(world, monkeypatch):
    project, slot = world
    monkeypatch.setattr(slots, "launch_mission", lambda *a: pytest.fail("must not open a terminal"))
    context = slots.current_session_context(str(project), "one", "hermes-session")
    assert Path(context["cwd"]) == slot.resolve()
    assert context["mode"] == "new" and not context["mission"]
    assert not (slot / "state/ACTIVE").exists()


def test_resume_preserves_owner_and_rejects_another_session(world):
    project, slot = world
    guard.write_active(str(slot / "state"), guard.Active("m-one", "original", "now", runtime="hermes"))
    with pytest.raises(UsageError):
        slots.current_session_context(str(project), "one", "other", resume=True)
    with pytest.raises(UsageError):
        slots.current_session_context(str(project), "one", "original")
    context = slots.current_session_context(str(project), "one", "original", resume=True)
    assert context["mission"] == "m-one" and context["mode"] == "resume"
    assert guard.read_active(str(slot / "state")).session == "original"


def test_failure_and_unknown_slot_cannot_enter(world):
    project, slot = world
    with pytest.raises(UsageError):
        slots.current_session_context(str(project), "missing", "s")
    located = slots.locate(str(project), "hermes")
    config = slots.Config(located, slots.read_adapter(located.adapter_path, "hermes"), "hermes")
    slots.Rows(config.rows_path).upsert("one", state="failed:setup")
    with pytest.raises(UsageError):
        slots.current_session_context(str(project), "one", "s")


def test_hermes_run_returns_actionable_error_without_terminal(world):
    project, _ = world
    assert slots.main(["--project", str(project), "--runtime", "hermes", "run", "one", "task"]) == slots.USAGE



@pytest.fixture
def registry_world(tmp_path, monkeypatch):
    """Chef-studio layout: in-tree manager adapter, registry-only slot adapter."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HAN_ADAPTERS_DIR", raising=False)
    project = tmp_path / "orchard"
    project.mkdir()
    folder = project / ".hermes/orchestrator"
    folder.mkdir(parents=True)
    adapter = {"schema": 1, "paths": {"stateDir": "state"}, "slots": {
        "enabled": True, "vcs": "none", "root": str(tmp_path / "slots"),
        "client": "test-{name}", "names": ["one"], "prepare": []}}
    (folder / "adapter.json").write_text(json.dumps(adapter))
    assert slots.main(["--project", str(project), "--runtime", "hermes", "create", "one"]) == 0
    slot = tmp_path / "slots/one"
    assert slot.is_dir()
    assert not (slot / ".hermes/orchestrator/adapter.json").exists()
    return project, slot, hermes_home


def test_registry_only_slot_enters_when_registered_under_hermes_home(registry_world):
    project, slot, hermes_home = registry_world
    registry = hermes_home / "chef/adapters"
    registry.mkdir(parents=True)
    (registry / "one/adapter.json").parent.mkdir(parents=True, exist_ok=True)
    (registry / "one/adapter.json").write_text(json.dumps(
        {"schema": 1, "paths": {"stateDir": "state"}}
    ))
    (registry / "projects.json").write_text(json.dumps(
        [{"adapter": "one", "roots": [str(slot)]}]
    ))
    context = slots.current_session_context(str(project), "one", "hermes-session")
    assert Path(context["cwd"]) == slot.resolve()
    assert context["mode"] == "new" and not context["mission"]


def test_missing_slot_registry_error_names_slot_and_tried_registry(registry_world):
    project, slot, hermes_home = registry_world
    with pytest.raises(UsageError) as raised:
        slots.current_session_context(str(project), "one", "hermes-session")
    text = str(raised.value)
    assert "Hermes 슬롯 어댑터를 찾지 못함" in text
    assert str(slot) in text
    assert "hermes-home" in text.replace("\\", "/")
    assert "chef/adapters" in text.replace("\\", "/")



def test_chef_slot_follows_binding_when_gateway_env_differs(tmp_path, monkeypatch):
    """Start writes the registry under HAN_ADAPTERS_DIR; chef_slot has only HERMES_HOME."""
    from chef.guards.slot_binding import read_slot_binding

    studio = tmp_path / "studio-registry"
    studio.mkdir()
    (studio / "one/adapter.json").parent.mkdir(parents=True)
    (studio / "one/adapter.json").write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"}}))
    (studio / "projects.json").write_text(json.dumps([{"adapter": "one", "roots": [str(tmp_path / "slots/one")]}]))
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(studio))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    project = tmp_path / "orchard"
    project.mkdir()
    folder = project / ".hermes/orchestrator"
    folder.mkdir(parents=True)
    adapter = {"schema": 1, "paths": {"stateDir": "state"}, "slots": {
        "enabled": True, "vcs": "none", "root": str(tmp_path / "slots"),
        "client": "test-{name}", "names": ["one"], "prepare": []}}
    (folder / "adapter.json").write_text(json.dumps(adapter))
    assert slots.main(["--project", str(project), "--runtime", "hermes", "create", "one"]) == 0
    slot = tmp_path / "slots/one"
    binding = read_slot_binding(str(slot))
    assert Path(binding["registry"]) == studio
    monkeypatch.delenv("HAN_ADAPTERS_DIR")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "gateway-home"))
    context = slots.current_session_context(str(project), "one", "hermes-session")
    assert Path(context["cwd"]) == slot.resolve()
    assert context["mode"] == "new"


def test_chef_slot_matches_missing_path_spelling_via_recorded_identity(tmp_path, monkeypatch):
    """Registry roots may use a /Users spelling that does not exist; the live slot does."""
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HAN_ADAPTERS_DIR", raising=False)
    project = tmp_path / "orchard"
    project.mkdir()
    folder = project / ".hermes/orchestrator"
    folder.mkdir(parents=True)
    slots_root = tmp_path / "slots"
    adapter = {"schema": 1, "paths": {"stateDir": "state"}, "slots": {
        "enabled": True, "vcs": "none", "root": str(slots_root),
        "client": "test-{name}", "names": ["one"], "prepare": []}}
    (folder / "adapter.json").write_text(json.dumps(adapter))
    missing = tmp_path / "Users" / "sample-user" / "workspaces" / "orchard-slots" / "one"
    registry = hermes_home / "chef/adapters"
    registry.mkdir(parents=True)
    (registry / "one/adapter.json").parent.mkdir(parents=True, exist_ok=True)
    (registry / "one/adapter.json").write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"}}))
    (registry / "projects.json").write_text(json.dumps([{"adapter": "one", "roots": [str(missing)]}]))
    assert slots.main(["--project", str(project), "--runtime", "hermes", "create", "one"]) == 0
    slot = slots_root / "one"
    (slot / ".p4config").write_text("P4CLIENT=test-one\n", encoding="utf-8")
    # Recreate binding after .p4config exists so the p4 client id is stored.
    from chef.slots.configuration import Config
    from chef.infrastructure.configuration import locate, read_adapter
    from chef.guards.slot_binding import record_slot_registry, read_slot_binding
    located = locate(str(project), "hermes")
    record_slot_registry(Config(located, read_adapter(located.adapter_path, "hermes"), "hermes"), "one")
    binding = read_slot_binding(str(slot))
    assert binding.get("adapter") == "one"
    assert any(item["kind"] == "p4Client" and item["value"] == "test-one" for item in binding["identities"])
    context = slots.current_session_context(str(project), "one", "hermes-session")
    assert Path(context["cwd"]) == slot.resolve()
