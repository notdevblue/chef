"""Exercise a shared process and two Hermes homes through native dispatch."""

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
assert request["clean"]
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

    def run(label, home):
        manager, slot = root / (label + "-manager"), root / (label + "-slot")
        manager.mkdir()
        slot.mkdir()
        log, reservation = manager / "events.jsonl", manager / "reservation.json"
        adapter_dir = home / "chef" / "adapters"
        adapter_path = adapter_dir / "orchard" / "adapter.json"
        write_json(adapter_path, {"schema": 1, "paths": {"stateDir": "state"}, "externalSlots": {
            "root": str(root), "provider": [sys.executable, str(provider)],
            "workspaces": {"fern": {"path": str(slot), "data": {
                "log": str(log), "reservation": str(reservation),
            }}},
        }})
        write_json(adapter_dir / "projects.json", [{"adapter": "orchard", "roots": [str(manager), str(slot)]}])
        request = {"task_id": label + "-task", "session_id": label + "-session"}
        token = set_hermes_home_override(str(home)) if home != gateway else None
        session = set_session_vars(session_id=request["session_id"], message_id=label + "-open", profile=home.name)
        try:
            manifest = parse_manifest_file(copied / "hermes/plugin.yaml", copied / "hermes", "user", "")
            plugins.get_plugin_manager()._load_plugin(manifest)
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
            opened = plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission open seed"}, **request)
            assert opened is None, opened
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
            clear_session_vars(session)
            session = set_session_vars(session_id=request["session_id"], message_id=label + "-close", profile=home.name)
            closed = plugins.resolve_pre_tool_block("terminal", {"command": "echo han-mission close seed"}, **request)
            assert closed is None, closed
            events = [json.loads(line) for line in log.read_text().splitlines()]
            transitions = [event for event in events if event[0] in ("acquire", "release")]
            print(json.dumps({"case": label, "journal": str(journal), "acquire_release": transitions,
                              "active_after_close": active.exists(), "journal_after_close": journal.exists(),
                              "lock_gate": str(journal.with_suffix(".lock.gate")),
                              "reservation_after_close": json.loads(reservation.read_text())}), flush=True)
            assert transitions == [[action, request["session_id"], label + "-open"] for action in ("acquire", "release")]
            assert not active.exists() and not journal.exists() and json.loads(reservation.read_text()) is None
            assert journal.parent == scope, journal
            assert journal.with_suffix(".lock.gate").is_file()
            assert os.environ["HERMES_HOME"] == str(gateway)
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
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    print(result.stdout, end="")
    assert result.returncode == 0, result.stdout + result.stderr
