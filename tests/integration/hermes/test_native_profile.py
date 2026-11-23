"""Exercise a shared process and two Hermes homes through native dispatch."""

import os
import subprocess
import sys

import pytest

from tests.paths import PLUGIN_ROOT


NATIVE_CHECK = r'''
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

source, distribution = map(Path, sys.argv[1:])
sys.path.insert(0, str(source))
with tempfile.TemporaryDirectory(prefix="chef-hermes-profiles-") as directory:
    root = Path(directory).resolve()
    gateway = root / "hermes"
    profile = gateway / "profiles" / "fern-bot"
    os.environ.update(HERMES_HOME=str(gateway), HERMES_BUNDLED_PLUGINS=str(root / "no-plugins"),
                      TERMINAL_CWD=str(root), TERMINAL_ENV="local")
    for key in ("HAN_ADAPTERS_DIR", "CLAUDE_PROJECT_DIR", "HERMES_SAFE_MODE", "HAN_GUARD_LOG"):
        os.environ.pop(key, None)
    copied = root / "distribution"
    shutil.copytree(distribution, copied, ignore=shutil.ignore_patterns("__pycache__"))
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli import plugins
    from hermes_cli.middleware import apply_tool_request_middleware
    from hermes_cli.plugins_manifest import parse_manifest_file
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.file_tools_paths import _resolve_path_for_task
    from tools.registry import registry

    provider = root / "provider.py"
    provider.write_text("""
import json, sys
from pathlib import Path
request = json.load(sys.stdin)
action = request["action"]
state = Path(request["data"]["reservation"])
reservation = json.loads(state.read_text()) if state.exists() else None
owner = [request.get("who"), request.get("key")]
assert action not in ("acquire", "release") or request["clean"]
with Path(request["data"]["log"]).open("a") as log:
    print(json.dumps([action, *owner]), file=log)
if action == "status":
    assert reservation is None
elif action == "acquire":
    assert reservation is None
    state.write_text(json.dumps(owner))
elif action == "check":
    assert reservation == owner
elif action == "release":
    assert reservation == owner
    state.write_text("null")
else:
    raise AssertionError(action)
print('{"ok":true}')
""", encoding="utf-8")

    def write_json(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def run_research(slot, request):
        from agent.subagent_lifecycle import bind_subagent_parent
        from tools import delegate_tool, delegate_tool_config
        from tools.delegate_tool_toolsets import _resolve_child_toolsets

        subprocess.run(["git", "init", "-q", str(slot)], check=True)
        subprocess.run(["git", "-c", "user.name=Fictional", "-c", "user.email=fictional@example.test",
                        "commit", "--allow-empty", "-qm", "Seed fictional workspace"], cwd=slot, check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=slot, text=True).strip()
        (slot / "state/seed/mission.md").write_text("# Fictional mission\n", encoding="utf-8")
        child = SimpleNamespace(session_id="fern-research-session", _subagent_id="sa-test", _delegate_role="leaf",
                                _delegate_depth=1, model="fictional-model", provider="fictional-provider")
        parent = SimpleNamespace(session_id=request["session_id"], enabled_toolsets=["chef", "chef-research", "file", "terminal"])

        def build(*, goal, toolsets, role, parent_agent, **_):
            assert _resolve_child_toolsets(parent_agent, toolsets, role)[0] == ["chef-research"]
            plugins.invoke_hook("subagent_start", parent_session_id=parent_agent.session_id,
                                child_session_id=child.session_id, child_subagent_id=child._subagent_id, child_goal=goal)
            return child

        def run_child(*_):
            child_request = {"session_id": child.session_id, "task_id": "fern-research-task"}
            assert get_hermes_home() == profile
            assert plugins.resolve_pre_tool_block("terminal", {"command": "echo blocked"}, **child_request)
            args = {"operation": "head"}
            assert plugins.resolve_pre_tool_block("chef_research_read", args, **request)
            assert plugins.resolve_pre_tool_block("chef_research_read", args, **child_request) is None
            observed = json.loads(registry.dispatch("chef_research_read", args, **child_request))
            assert observed == {"head": head}, observed
            plugins.invoke_hook("subagent_stop", parent_session_id=parent.session_id, child_session_id=child.session_id)
            return {"status": "completed", "summary": observed["head"]}

        # Exercise the real lifecycle service and native registry; use a fake
        # child/model call so this test needs no provider or credentials.
        with (bind_subagent_parent(parent),
              patch.object(delegate_tool_config, "_resolve_delegation_credentials", return_value={"model": child.model}),
              patch.object(delegate_tool, "_credential_overrides", return_value={}),
              patch.object(delegate_tool, "_build_child_preserving_parent_tools", side_effect=build),
              patch.object(delegate_tool, "_run_child_lifecycle", side_effect=run_child),
              patch("chef.runtimes.hermes.workers.codex_executable", side_effect=AssertionError("Codex was used"))):
            result = json.loads(registry.dispatch("chef_delegate", {"role": "chef:researcher", "prompt": "Find HEAD"}, **request))
        assert result["ok"] and result["result"] == head and result["runtime"] == "hermes", result

    def run(label, home):
        manager, slot = root / (label + "-manager"), root / (label + "-slot")
        manager.mkdir()
        slot.mkdir()
        log, reservation = manager / "events.jsonl", manager / "reservation.json"
        inspect = manager / "inspect.py"
        inspect.write_text("""
import json, sys
from pathlib import Path
if sys.argv[1] == "status":
    state = Path(sys.argv[2])
    print("LOCKED" if state.exists() and json.loads(state.read_text()) else "UNLOCKED")
elif sys.argv[1] == "list":
    print("WORKSPACES " + Path.cwd().name)
""", encoding="utf-8")
        status_argv = [sys.executable, str(inspect), "status", str(reservation)]
        list_argv = [sys.executable, str(inspect), "list"]
        adapter_dir = home / "chef" / "adapters"
        adapter_path = adapter_dir / "orchard" / "adapter.json"
        write_json(adapter_path, {"schema": 1, "paths": {"stateDir": "state"}, "externalSlots": {
            "root": str(root), "provider": [sys.executable, str(provider)],
            "readOnlyCommands": [status_argv, list_argv],
            "workspaces": {"fern": {"path": str(slot), "data": {
                "log": str(log), "reservation": str(reservation),
            }}},
        }})
        write_json(adapter_dir / "projects.json", [{"adapter": "orchard", "roots": [str(manager), str(slot)]}])
        if home != gateway:
            write_json(adapter_dir / "orchard" / "local.json", {"runtimes": {"hermes": {"workers": {"backend": "hermes"}}}})
            (home / "config.yaml").write_text("delegation:\n  inherit_mcp_toolsets: false\n", encoding="utf-8")
        request = {"task_id": label + "-task", "session_id": label + "-session"}
        token = set_hermes_home_override(str(home)) if home != gateway else None
        session = set_session_vars(session_id=request["session_id"], message_id=label + "-open", profile=home.name)
        try:
            manifest = parse_manifest_file(copied / "hermes/plugin.yaml", copied / "hermes", "user", "")
            plugins.get_plugin_manager()._load_plugin(manifest)

            def terminal(command, cwd=None):
                incoming = {"command": command}
                if cwd is not None:
                    incoming["workdir"] = str(cwd)
                rewritten = apply_tool_request_middleware("terminal", incoming, **request).payload
                deny = plugins.resolve_pre_tool_block("terminal", rewritten, **request)
                if deny:
                    return {"blocked": deny}
                return json.loads(registry.dispatch("terminal", rewritten, **request))

            args = {"project": str(manager), "name": "fern"}
            dry_run = json.loads(registry.dispatch("chef_slot", {**args, "dry_run": True}, **request))
            print(json.dumps({"case": label, "process_home": os.environ["HERMES_HOME"],
                              "request_home": str(get_hermes_home()), "dry_run": dry_run}), flush=True)
            assert dry_run.get("cwd") == str(slot) and dry_run["dry_run"], dry_run
            active = slot / "state" / "ACTIVE"
            assert not active.exists() and not reservation.exists()
            assert str(_resolve_path_for_task(".", request["task_id"])) == str(root)
            bound = json.loads(registry.dispatch("chef_slot", args, **request))
            assert bound.get("cwd") == str(slot) and bound["external"], bound
            assert str(_resolve_path_for_task(".", request["task_id"])) == str(slot)
            if home != gateway:
                from chef.cli import mission_entry

                worker_status = json.loads(registry.dispatch("chef_worker_status", {}, **request))
                assert worker_status == {"available": True, "backend": "hermes", "roles": ["chef:researcher"]}, worker_status
                entry = root / "entry.py"
                entry.write_text(f"print({json.dumps({'schema': 1, 'action': 'continue'})!r})", encoding="utf-8")
                write_json(adapter_dir / "mission-entry.json", {"schema": 1, "events": [{
                    "id": "fern-entry", "roots": [str(slot)], "trusted": True,
                    "command": [sys.executable, str(entry)], "watchFiles": [str(entry)],
                }]})
                denied = plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission open seed"}, **request)
                assert denied and "mission-entry-receipts" in denied, denied
                assert not active.exists() and not reservation.exists()
                receipt = mission_entry.enter(str(slot), "hermes", "new", request["session_id"], "seed", "Fictional mission")
                assert Path(receipt["receipt"]).is_relative_to(adapter_dir)
                print(json.dumps({"case": label, "missing_receipt_blocked": True,
                                  "acquired_before_receipt": reservation.exists()}), flush=True)
            opened = terminal("echo han-mission open seed")
            assert opened.get("exit_code") == 0, opened
            assert json.loads(active.read_text())["session"] == request["session_id"]
            scope = home / "chef" / "external-slots"
            journals = [p for p in (gateway / "chef" / "external-slots").glob("*.json")]
            if home != gateway:
                journals.extend(scope.glob("*.json"))
            assert len(journals) == 1, journals
            journal = journals[0]
            if home != gateway:
                denied = plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission resume seed"}, **request)
                assert denied and "mission-entry-receipts" in denied, denied
                mission_entry.enter(str(slot), "hermes", "resume", request["session_id"], "seed", "Fictional mission")
                resumed = plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission resume seed"}, **request)
                assert resumed is None, resumed
                print(json.dumps({"case": label, "resume_requires_separate_receipt": True}), flush=True)
                run_research(slot, request)
            clear_session_vars(session)
            session = set_session_vars(session_id=request["session_id"], message_id=label + "-close", profile=home.name)
            closed = terminal("echo han-mission close seed")
            assert closed.get("exit_code") == 0, closed
            events = [json.loads(line) for line in log.read_text().splitlines()]
            transitions = [event for event in events if event[0] in ("acquire", "release")]
            print(json.dumps({"case": label, "journal": str(journal), "acquire_release": transitions,
                              "open_exit": opened["exit_code"], "close_exit": closed["exit_code"],
                              "active_after_close": active.exists(), "journal_after_close": journal.exists(),
                              "lock_gate": str(journal.with_suffix(".lock.gate")),
                              "reservation_after_close": json.loads(reservation.read_text())}), flush=True)
            assert transitions == [[action, request["session_id"], label + "-open"] for action in ("acquire", "release")]
            assert not active.exists() and not journal.exists() and json.loads(reservation.read_text()) is None
            assert journal.parent == scope, journal
            assert journal.with_suffix(".lock.gate").is_file()
            assert os.environ["HERMES_HOME"] == str(gateway)
            if home != gateway:
                status = terminal(shlex.join(status_argv), slot)
                listing = terminal(shlex.join(list_argv), manager)
                restored = str(_resolve_path_for_task(".", request["task_id"]))
                print(json.dumps({"case": label, "post_close": {"slot_status": status, "manager_list": listing,
                                                               "native_cwd": restored}}), flush=True)
                assert status.get("exit_code") == 0 and status["output"].strip() == "UNLOCKED", status
                assert listing.get("exit_code") == 0 and listing["output"].strip() == "WORKSPACES " + manager.name, listing
                assert restored == str(root), restored
                status = json.loads(registry.dispatch("chef_slot", {**args, "dry_run": True}, **request))
                assert status.get("reservation") == "UNLOCKED", status
        finally:
            clear_session_vars(session)
            if token is not None:
                reset_hermes_home_override(token)

    run("default", gateway)
    run("profile", profile)
    assert get_hermes_home() == gateway
'''


@pytest.mark.skipif(not os.environ.get("HERMES_AGENT_SOURCE"), reason="set HERMES_AGENT_SOURCE for real native dispatch")
def test_native_external_slot_uses_profile_from_lookup_through_release(tmp_path):
    result = subprocess.run(
        [os.environ.get("HERMES_AGENT_PYTHON", sys.executable), "-c", NATIVE_CHECK,
         os.environ["HERMES_AGENT_SOURCE"], str(PLUGIN_ROOT)],
        cwd=tmp_path, capture_output=True, text=True, timeout=90,
    )
    print(result.stdout, end="")
    assert result.returncode == 0, result.stdout + result.stderr
