"""Prepare a bounded transition in the hook; commit only in the executed CLI.

Receipts connect the native hook identity to one command. They are cooperative
tool policy, not an OS sandbox or a replacement for the host permission check.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from chef.infrastructure.storage import atomic_write_json, make_private_directory
from chef.infrastructure.fingerprints import execution_digest
from chef.locations import PLUGIN_ROOT
from chef.guards.adapters import AdapterNotFound, GuardOff

TTL_SECONDS = 120
SCRIPT = PLUGIN_ROOT / "mission_control.py"


def parser():
    value = argparse.ArgumentParser(allow_abbrev=False)
    value.add_argument("action", choices=("open", "resume", "close"))
    value.add_argument("mission")
    value.add_argument("--runtime", required=True, choices=("claude", "codex", "grok"))
    value.add_argument("--session", required=True)
    value.add_argument("--project", required=True)
    value.add_argument("--request", required=True)
    return value


def arguments(argv):
    # Do not let argparse terminate the hook on malformed input.
    try:
        data = vars(parser().parse_args(argv))
    except SystemExit as error:
        raise ValueError("invalid mission_control.py arguments") from error
    from chef.guards.adapters import valid_mission_id

    if not valid_mission_id(data["mission"]) or not data["session"]:
        raise ValueError("mission and session must be valid and nonempty")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", data["request"]):
        raise ValueError("--request must be a fresh 8-80 character identifier")
    if not Path(data["project"]).is_absolute() or not Path(data["project"]).is_dir():
        raise ValueError("--project must be an existing absolute directory")
    data["project"] = os.path.realpath(data["project"])
    return data


def invocation(command, tool):
    from chef.guards.commands import control_invocation, readonly, tokens
    from chef.guards.shell import command_fragments
    from chef.infrastructure.paths import same_path

    found = [(script, argv) for script, argv in control_invocation(command, tool)
             if re.split(r"[\\/]", script)[-1].casefold() == "mission_control.py"]
    if not found:
        if "mission_control.py" in command.casefold() and not readonly(command, tool):
            raise ValueError("mission_control.py must run as a literal standalone command")
        return None
    if len(found) != 1 or len(command_fragments(command, tool)) != 1:
        raise ValueError("mission_control.py must run alone")
    script, argv = found[0]
    values = tokens(command)
    masked = re.sub(r'''"[^"\r\n]*"|'[^'\r\n]*' ''', "", command, flags=re.VERBOSE)
    masked = masked.lstrip().removeprefix("&").lstrip()
    if (any(char in masked for char in ";&|><(){}\n\r") or "$" in command or "`" in command
            or not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?",
                                re.split(r"[\\/]", values[0])[-1], re.IGNORECASE)
            or values[1:-len(argv)] not in ([script], ["-B", script])):
        raise ValueError("use a literal standalone Python mission_control.py command")
    if not Path(script).is_absolute() or not same_path(script, SCRIPT):
        raise ValueError("mission_control.py must use this installed plugin's absolute path")
    return arguments(argv)


def marker_payload(payload, data):
    return {**payload, "tool_name": "Bash", "tool_input": {
        "command": f"echo han-mission {data['action']} {data['mission']}"}}


def location(payload):
    from chef.guards import mission

    located = mission.find_adapter(payload)
    adapter = mission.load_adapter(located.adapter_path, runtime=payload["han_runtime"])
    state = os.path.realpath(mission.state_dir_of(located.project, adapter))
    return located, adapter, state


def snapshot(payload):
    from chef.guards import mission

    located, adapter, state = location(payload)
    fingerprint = hashlib.sha256(json.dumps(adapter, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"project": os.path.realpath(located.project), "adapter": os.path.realpath(located.adapter_path),
            "state": state, "adapterDigest": fingerprint, "active": mission.read_active(state)._asdict()}


def receipt_path(state, request):
    return Path(state) / ".transitions" / (request + ".json")


def prepare(payload, data):
    from chef.cli.mission_entry import active_lock
    from chef.guards import mission
    from chef.infrastructure.paths import same_path

    if (not isinstance(payload.get("cwd"), str) or not payload["cwd"]
            or payload.get("han_runtime") != data["runtime"] or payload.get("session_id") != data["session"]
            or not same_path(payload.get("cwd") or "", data["project"])):
        raise ValueError("transition command differs from the native hook session, runtime or cwd")
    if payload.get("agent_id") or payload.get("han_worker") or payload.get("han_hermes_worker"):
        raise ValueError("role workers cannot change mission ownership")
    checked_payload = marker_payload(payload, data)
    _, _, state = location(checked_payload)
    with active_lock(state):
        before = snapshot(checked_payload)
        outcome = mission.decide(checked_payload, marker_mode="check", marker_locked=True)
        if outcome.deny:
            return outcome
        if snapshot(checked_payload) != before:
            raise ValueError("transition inputs changed during the hook check")
        path = receipt_path(state, data["request"])
        if path.exists():
            raise ValueError("transition request was already prepared; use a fresh --request")
        make_private_directory(path.parent)
        atomic_write_json(path, {"schema": 1, "createdAt": time.time(), "request": data,
                                 "payload": checked_payload, "before": before,
                                 "implementationDigest": execution_digest()}, durable=True)
    return mission.Outcome(messages=["Mission transition checked; ACTIVE is unchanged until the command executes"])


def execute(data):
    from chef.cli.mission_entry import active_lock
    from chef.guards import mission
    from chef.infrastructure.paths import same_path

    if os.environ.get("CHEF_WORKER_CONTEXT") or os.environ.get("CHEF_HERMES_WORKER"):
        raise ValueError("role workers cannot execute mission transitions")
    if not same_path(os.getcwd(), data["project"]):
        raise ValueError("execute the transition from its checked project directory")
    probe = {"cwd": data["project"], "han_runtime": data["runtime"], "session_id": data["session"]}
    _, _, state = location(probe)
    path = receipt_path(state, data["request"])
    with active_lock(state):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - receipt.get("createdAt", 0)
        if (receipt.get("schema") != 1 or receipt.get("request") != data or not 0 <= age <= TTL_SECONDS
                or receipt.get("consumed") or receipt.get("implementationDigest") != execution_digest()):
            raise ValueError("transition receipt is stale, consumed or bound to another command")
        payload = receipt["payload"]
        if receipt.get("before") != snapshot(payload):
            raise ValueError("ACTIVE, adapter or route changed after the hook check; prepare a new request")
        # Consume before any mutation. A failed command is never silently replayed.
        atomic_write_json(path, {**receipt, "consumed": True}, durable=True)
        outcome = mission.decide(payload, marker_mode="execute", marker_locked=True,
                                 marker_snapshot=receipt["before"])
        if outcome.deny:
            raise ValueError(outcome.deny)
        after = mission.read_active(state)
        if data["action"] in ("open", "resume") and (
                after.id != data["mission"] or after.session != data["session"] or after.runtime != data["runtime"]):
            raise ValueError("transition did not establish the requested ownership")
        if data["action"] == "close" and after.id:
            raise ValueError("transition did not close the mission")
        return outcome


def main(argv=None):
    try:
        result = execute(arguments(sys.argv[1:] if argv is None else argv))
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AdapterNotFound, GuardOff) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "messages": result.messages}, ensure_ascii=False))
    return 0
