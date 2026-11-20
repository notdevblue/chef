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
