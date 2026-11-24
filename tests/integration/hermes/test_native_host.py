"""Opt-in contract test against an actual public Hermes source installation.

Set HERMES_AGENT_SOURCE to the Hermes checkout and optionally
HERMES_AGENT_PYTHON to its Python interpreter. No existing Hermes home,
credentials, model provider, or messaging connection is used.
"""

import json
import os
import subprocess
import sys

import pytest

from tests.paths import PLUGIN_ROOT


NATIVE_CHECK = r'''
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

source, distribution = map(Path, sys.argv[1:])
sys.path.insert(0, str(source))
with tempfile.TemporaryDirectory(prefix="chef-hermes-native-host-") as directory:
    root = Path(directory).resolve()
    os.environ["HERMES_HOME"] = str(root / "hermes-home")
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(root / "empty-plugins")
    os.environ["HAN_ADAPTERS_DIR"] = str(root / "empty-registry")
    for key in ("CLAUDE_PROJECT_DIR", "HERMES_SAFE_MODE", "HERMES_ENABLE_PROJECT_PLUGINS", "HAN_GUARD_LOG"):
        os.environ.pop(key, None)
    copied = root / "distribution"
    shutil.copytree(distribution, copied, ignore=shutil.ignore_patterns("__pycache__"))
    project = root / "comet"
    registration = project / ".hermes/orchestrator"
    registration.mkdir(parents=True)
    adapter = {
        "schema": 1, "project": "Comet", "paths": {"stateDir": "state", "vcsRoot": "."},
        "guard": {"coordinatorWrite": ["state/**"], "deny": [{"pattern": "dangerous-operation", "reason": "fictional operation denied"}]},
        "slots": {"enabled": True, "vcs": "none", "root": str(root / "slots"), "client": "comet-{name}", "names": ["cli", "gateway", "routed"], "prepare": []},
    }
    (registration / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    os.environ["TERMINAL_CWD"] = str(project)
    os.environ["TERMINAL_ENV"] = "local"
    from hermes_cli import plugins
    manager = plugins.PluginManager()
    plugins._plugin_manager = manager
    try:
        from hermes_cli.plugins_manifest import parse_manifest_file
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_cli.plugins_manifest":
            raise
        parse_manifest_file = manager._parse_manifest
    manifest = parse_manifest_file(copied / "hermes/plugin.yaml", copied / "hermes", "user", "")
    manager._load_plugin(manifest)
    loaded = manager._plugins["chef"]
    assert loaded.enabled and not loaded.error, loaded.error
    assert set(loaded.tools_registered) == {"chef_slot", "chef_delegate", "chef_worker_status", "chef_research_read",
                                           "chef_worker_read", "chef_worker_write", "chef_worker_shell"}
    assert set(loaded.hooks_registered) == {"pre_tool_call", "pre_llm_call", "pre_verify", "subagent_start", "subagent_stop"}
    assert loaded.middleware_registered == ["tool_request"]
    assert set(manager._plugin_skills) == {"chef:mission", "chef:slots", "chef:adapter-init", "chef:doctor", "chef:distill"}
    from chef.slots import commands
    assert commands.main(["--project", str(project), "--runtime", "hermes", "create", "cli", "gateway", "routed"]) == 0
    from chef.guards import mission
    from hermes_cli.middleware import apply_tool_request_middleware
    try:
        from tools.file_tools_paths import _resolve_path_for_task
    except ModuleNotFoundError as exc:
        if exc.name != "tools.file_tools_paths":
            raise
        from tools.file_tools import _resolve_path_for_task
    from tools.registry import registry
    foreign = root / "foreign\\garden"
    foreign_registration = foreign / ".hermes/orchestrator"
    foreign_registration.mkdir(parents=True)
    (foreign_registration / "adapter.json").write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"}}))
    (foreign / "state").mkdir()
    (foreign / "state/ACTIVE").write_text(json.dumps({"id": "foreign-mission", "session": "foreign-owner", "runtime": "hermes"}))
    for surface in ("cli", "gateway"):
        task, session = surface + "-task", surface + "-session"
        slot = root / "slots" / surface
        slot_registration = slot / ".hermes/orchestrator"
        slot_registration.mkdir(parents=True)
        (slot_registration / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
        result = json.loads(registry.dispatch("chef_slot", {"project": str(project), "name": surface}, task_id=task, session_id=session))
        assert result.get("cwd") == str(slot), result
        assert str(_resolve_path_for_task(".", task)) == str(slot)
        rewritten = apply_tool_request_middleware("terminal", {"command": "echo safe"}, task_id=task, session_id=session)
        assert rewritten.payload["workdir"] == str(slot)
        rewritten_file = apply_tool_request_middleware("write_file", {"path": "note.txt", "content": "fictional"}, task_id=task, session_id=session)
        assert rewritten_file.payload["path"] == str(slot / "note.txt")
        assert plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission open native-mission"}, task_id=task, session_id=session) is None
        active = mission.read_active(str(slot / "state"))
        assert active.session == session and active.runtime == "hermes"
        denied = plugins.resolve_pre_tool_block("terminal", {"command": "dangerous-operation"}, task_id=task, session_id=session)
        assert denied and "fictional operation denied" in denied, denied
        assert plugins.resolve_pre_tool_block("terminal", {"command": "echo safe"}, task_id=task, session_id=session) is None
        foreign = plugins.resolve_pre_tool_block("terminal", {"command": "echo safe"}, task_id=task, session_id="unrelated")
        assert foreign and "another active" in foreign, foreign
        foreign_path = root / "foreign\\garden/probe.txt"
        assert str(_resolve_path_for_task(str(foreign_path), task)) == str(foreign_path)
        foreign_write = plugins.resolve_pre_tool_block("write_file", {"path": str(foreign_path), "content": "fixture"}, task_id=task, session_id=session)
        assert foreign_write and "another active" in foreign_write, foreign_write
        assert not foreign_path.exists()
        write = plugins.resolve_pre_tool_block("write_file", {"path": "source.py", "content": "fictional"}, task_id=task, session_id=session)
        assert write and "roles.implement" in write, write
        assert any(item.get("context") for item in manager.invoke_hook("pre_llm_call", task_id=task, session_id=session))

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    os.environ.pop("HAN_ADAPTERS_DIR")
    slot = root / "slots" / "routed"
    profile = root / "hermes-home" / "profiles" / "comet-bot"
    profile_registry = profile / "chef" / "adapters"
    registered_adapter = profile_registry / "comet" / "adapter.json"
    registered_adapter.parent.mkdir(parents=True)
    registered_adapter.write_text(json.dumps(adapter), encoding="utf-8")
    (profile_registry / "projects.json").write_text(json.dumps([{"adapter": "comet", "roots": [str(slot)]}]), encoding="utf-8")
    args = {"project": str(project), "name": "routed"}
    request = {"task_id": "routed-task", "session_id": "routed-session"}
    token = set_hermes_home_override(str(profile))
    try:
        plugins.get_plugin_manager()._load_plugin(manifest)
        result = json.loads(registry.dispatch("chef_slot", {**args, "dry_run": True}, **request))
        assert result.get("cwd") == str(slot) and result["dry_run"], result
        assert str(_resolve_path_for_task(".", request["task_id"])) == str(project)
        result = json.loads(registry.dispatch("chef_slot", args, **request))
        assert result.get("cwd") == str(slot), result
        assert plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission open routed-mission"}, **request) is None
        assert mission.read_active(str(slot / "state")).session == request["session_id"]
        denied = plugins.resolve_pre_tool_block("terminal", {"command": "dangerous-operation"}, **request)
        assert denied and "fictional operation denied" in denied, denied
    finally:
        reset_hermes_home_override(token)
    assert os.environ["HERMES_HOME"] == str(root / "hermes-home")
    result = json.loads(registry.dispatch("chef_slot", {"project": str(slot), "name": "routed", "dry_run": True}, task_id="default-task", session_id="default-session"))
    assert str(root / "hermes-home" / "chef" / "adapters") in result["error"], result
    print(json.dumps({"native_plugin": "passed", "surfaces": ["cli", "gateway"], "guards": "passed", "task_cwd_isolation": "passed", "profile_registry": "passed", "tools": 7, "hooks": 5, "skills": 5}))
'''


@pytest.mark.skipif(not os.environ.get("HERMES_AGENT_SOURCE"), reason="set HERMES_AGENT_SOURCE for the real native-host contract")
def test_real_native_plugin_host_preserves_trusted_cli_and_gateway_context(tmp_path):
    result = subprocess.run(
        [os.environ.get("HERMES_AGENT_PYTHON", sys.executable), "-c", NATIVE_CHECK,
         os.environ["HERMES_AGENT_SOURCE"], str(PLUGIN_ROOT)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["guards"] == report["task_cwd_isolation"] == report["profile_registry"] == "passed"
