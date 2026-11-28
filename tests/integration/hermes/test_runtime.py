"""Runtime contracts using entirely fictional projects and isolated homes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.cli import check_guard, doctor, distill, mission_entry
from chef.guards import adapters, mission, subagents
from chef.mission import state
from chef.mission.contracts import Context
from chef.runtimes import hooks
from chef.slots import contracts


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for key in (
        "HAN_ADAPTERS_DIR", "HAN_REPO", "CLAUDE_PROJECT_DIR",
        "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR", "CLAUDE_ENV_FILE", "HAN_GUARD_LOG",
        "HAN_DISTILL_LOG", "GROK_HOOK_EVENT", "GROK_SESSION_ID", "HERMES_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(adapters, "in_place_adapters_dir", lambda: str(tmp_path / "absent"))


def adapter(project, extra=None, kind=".hermes"):
    return write_json(project / kind / "orchestrator" / "adapter.json", {
        "schema": 1,
        "project": "fictional-garden",
        "paths": {"stateDir": "state"},
        **(extra or {}),
    })


def payload(project, tool, tool_input, **extra):
    return {
        "han_runtime": "hermes", "hook_event_name": "PreToolUse",
        "session_id": "garden-owner", "cwd": str(project),
        "tool_name": tool, "tool_input": tool_input, **extra,
    }


def test_nearest_hermes_adapter_precedes_other_runtime_and_ambient_project(tmp_path, monkeypatch):
    project = tmp_path / "garden"
    child = project / "src"
    child.mkdir(parents=True)
    adapter(tmp_path, kind=".hermes")
    adapter(project, kind=".claude")
    selected = adapter(project)
    other = tmp_path / "other"
    adapter(other)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    found = adapters.find_adapter({"cwd": str(child), "han_runtime": "hermes"})
    assert Path(found.project) == project
    assert Path(found.adapter_path) == selected


def test_hermes_overlay_and_local_overlay_are_applied_to_same_adapter(tmp_path):
    selected = adapter(tmp_path, {
        "vars": {"common": "base"},
        "runtimes": {"hermes": {"project": "hermes-garden", "vars": {"runtime": "base"}}},
    })
    write_json(selected.parent / "local.json", {
        "vars": {"common": "local"},
        "runtimes": {"hermes": {"vars": {"runtime": "local"}}},
    })
    problems = []
    loaded = adapters.load_adapter(str(selected), problems, "hermes")
    assert not problems
    assert loaded["project"] == "hermes-garden"
    assert loaded["vars"] == {"common": "local", "runtime": "local"}
    assert adapters.load_adapter(str(selected), [], "claude")["project"] == "fictional-garden"


def test_registry_uses_explicit_environment_before_hermes_home(tmp_path, monkeypatch):
    home_registry = tmp_path / "hermes-home" / "chef" / "adapters"
    home_registry.mkdir(parents=True)
    assert adapters.resolve_registry("hermes") == adapters.Registry(str(home_registry), "hermesHome")
    selected = tmp_path / "explicit-adapters"
    selected.mkdir()
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(selected))
    assert adapters.resolve_registry("hermes") == adapters.Registry(str(selected), "env")


def test_canonical_bridge_payload_keeps_owner_and_does_not_trust_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "ambient-session")
    original = payload(tmp_path, "Bash", {"command": "python task.py"}, agent_id="trusted-child")
    normalized = hooks.normalize_payload(original)
    assert normalized["session_id"] == "garden-owner"
    assert normalized["agent_id"] == "trusted-child"
    assert normalized["tool_input"] == original["tool_input"]
    assert hooks.runtime_of({}) == "claude"


def test_hermes_owner_marker_records_runtime_and_command_guard_applies(tmp_path):
    adapter(tmp_path, {"guard": {"deny": [{"pattern": "forbidden-action", "reason": "fictional restriction"}]}})
    outcome = mission.decide(payload(tmp_path, "Bash", {"command": "echo han-mission open garden-mission"}), marker_mode="execute")
    assert not outcome.deny
    active = mission.read_active(str(tmp_path / "state"))
    assert (active.id, active.session, active.runtime) == ("garden-mission", "garden-owner", "hermes")
    outcome = mission.decide(payload(tmp_path, "Bash", {"command": "forbidden-action"}))
    assert outcome.deny.startswith("fictional restriction")


def test_trusted_child_cannot_replace_parent_active(tmp_path):
    adapter(tmp_path)
    write_json(tmp_path / "state" / "ACTIVE", {
        "id": "garden-mission", "session": "garden-owner", "openedAt": "2026-01-01", "runtime": "hermes",
    })
    outcome = mission.decide(payload(tmp_path, "Bash", {"command": "echo han-mission close garden-mission"}, agent_id="trusted-child"))
    assert outcome.deny and "Hermes" in outcome.deny
    assert mission.read_active(str(tmp_path / "state")).id == "garden-mission"


def test_canonical_hermes_delegation_checks_chef_role(tmp_path):
    adapter(tmp_path, {"agents": {"allow": ["chef:researcher"]}})
    assert not subagents.decide(payload(tmp_path, "delegate_task", {"subagent_type": "chef:researcher"})).deny
    assert subagents.decide(payload(tmp_path, "delegate_task", {"subagent_type": "other-role"})).deny


def test_hermes_transcripts_do_not_guess_another_runtime_file(tmp_path, monkeypatch):
    folder = tmp_path / "state" / "garden-mission"
    folder.mkdir(parents=True)
    (folder / "sessions.tsv").write_text("at\taction\tsession\ttranscript\nnow\topen\tgarden-owner\t\n", encoding="utf-8")
    monkeypatch.setattr(state, "search_claude", lambda *args: pytest.fail("must not inspect another runtime"))
    context = Context({}, str(folder.parent), folder.name, "hermes", str(tmp_path))
    assert state.cmd_transcripts(context, SimpleNamespace()) == ["없음: garden-owner"]


def test_runtime_choices_and_hermes_distill_home(tmp_path):
    assert "hermes" in contracts.RUNTIMES
    assert "hermes" not in contracts.TERMINAL_RUNTIMES
    assert "hermes" in mission_entry.RUNTIMES
    assert check_guard.consume_runtime(["--runtime", "hermes"], "claude")[1] == "hermes"
    assert Path(distill.log_path("hermes")).is_relative_to(tmp_path / "hermes-home")
    report = doctor.Report()
    selected = adapter(tmp_path, {"runtimes": {"hermes": {"rolePolicy": {"model": "definition", "subagents": True}}}})
    doctor.check_role_policy(report, "garden", str(selected))
    assert not report.failed


def test_hermes_registry_uses_home_even_when_folder_is_missing(tmp_path):
    expected = tmp_path / "hermes-home" / "chef" / "adapters"
    assert not expected.exists()
    assert adapters.resolve_registry("hermes") == adapters.Registry(str(expected), "hermesHome")


def test_in_place_registry_is_not_used_for_a_foreign_project(tmp_path, monkeypatch):
    repo_adapters = tmp_path / "chef-repo" / "adapters"
    repo_adapters.mkdir(parents=True)
    monkeypatch.setattr(adapters, "in_place_adapters_dir", lambda: str(repo_adapters))
    foreign = tmp_path / "other-project"
    foreign.mkdir()
    assert adapters.resolve_registry("claude", project=str(foreign)) == adapters.Registry("", "none")
    inside = tmp_path / "chef-repo" / "src"
    inside.mkdir()
    assert adapters.resolve_registry("claude", project=str(inside)) == adapters.Registry(
        str(repo_adapters), "in-place"
    )
    message = adapters.registry_missing_message("claude", project=str(foreign))
    assert "in-place" in message and "건너뜀" in message
