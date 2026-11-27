"""Per-attempt hook context connecting an external CLI to its parent mission.

This is a cooperative tool policy. It does not sandbox arbitrary helper programs.
"""

import json
import os
from pathlib import Path

from chef.infrastructure.fingerprints import execution_digest as implementation_digest
from chef.infrastructure.locking import process_alive
from chef.infrastructure.paths import inside_real_root, same_path

ENV = "CHEF_WORKER_CONTEXT"
PROBE = "echo han-mission open chef-worker-probe"
READ_ONLY = ("researcher", "code-reviewer", "reviewer-small")


def read():
    path = os.environ.get(ENV)
    if not path:
        return None
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (value.get("schema") != 1 or not process_alive(value["pid"])
            or value.get("digest") != implementation_digest()):
        raise ValueError("expired or incompatible Chef worker context")
    cancel_path = Path(value.get("cancelPath") or Path(path).parent / "cancel.json")
    if value.get("revoked") or cancel_path.exists():
        raise ValueError("Chef worker permission was revoked")
    owner = value["owner"]
    from chef.guards import mission

    active = mission.read_active(owner["state"])
    if (active.corrupt or active.id != owner["mission"] or active.session != owner["session"]
            or active.runtime not in ("", owner["runtime"])):
        raise ValueError("Chef worker parent ownership changed")
    return value


def normalize(payload, runtime):
    data = read()
    if data is None:
        return payload
    if payload.get("han_worker"):
        return payload
    if data["backend"] != runtime:
        raise ValueError("worker hook runtime differs from assigned backend")
    cwd = os.path.realpath(payload.get("cwd") or data["cwd"])
    if not same_path(cwd, data["cwd"]) and not inside_real_root(data["cwd"], cwd):
        raise ValueError("worker tool cwd escaped its workspace")
    inputs = payload.get("tool_input")
    if isinstance(inputs, dict):
        for key in ("cwd", "workdir"):
            if inputs.get(key):
                target = os.path.realpath(os.path.join(cwd, inputs[key]))
                if not same_path(target, data["cwd"]) and not inside_real_root(data["cwd"], target):
                    raise ValueError("worker tool working directory escaped its workspace")
    if payload.get("hook_event_name") == "SessionStart":
        Path(data["ready"]).write_text(data["digest"], encoding="utf-8")
    owner = data["owner"]
    return {**payload, "session_id": owner["session"], "han_runtime": owner["runtime"],
            "agent_id": "chef-" + data["backend"] + "-worker", "cwd": cwd,
            "han_worker": data}


def acknowledge(payload, denied):
    data = payload.get("han_worker")
    inputs = payload.get("tool_input") or {}
    if data and denied and isinstance(inputs, dict) and inputs.get("command") == PROBE:
        Path(data["guardReady"]).write_text(data["digest"], encoding="utf-8")
    if data and denied and data.get("phase") == "work" and data.get("contextPath"):
        from chef.infrastructure.storage import atomic_write_json

        target = Path(data["contextPath"]).parent / "blocked.json"
        previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        count = previous.get("count", 0) + 1 if previous.get("reason") == denied else 1
        atomic_write_json(target, {"reason": denied, "command": inputs.get("command", ""), "count": count})


def denial(payload):
    data = payload.get("han_worker")
    if not data:
        return ""
    from chef.guards import mission, questions
    from chef.runtimes.hermes.workers import project_denial

    tool = str(payload.get("tool_name") or "")
    inputs = payload.get("tool_input") or {}
    if not isinstance(inputs, dict):
        return "worker tool input must be an object"
    command = str(inputs.get("command") or "")
    if data["phase"] == "probe":
        return "Chef worker guard probe denied"
    if data["phase"] != "work":
        return "worker context phase is invalid"
    if tool in ("Agent", "Task", "spawn_subagent") or tool.endswith("spawn_agent"):
        return "role workers cannot delegate more work"
    if inputs.get("run_in_background"):
        return "role workers cannot detach background commands"
    from chef.guards.commands import control_invocation, readonly

    control_read = False
    for script, arguments in control_invocation(command, tool):
        from chef.workers.verification import permitted, control_read as query

        if permitted(script, arguments, data):
            continue
        if not query(script, arguments):
            return "role workers cannot change mission or delegation state"
        control_read = len(mission.command_fragments(command, tool)) == 1 and not any(char in command for char in ">&<")
    allowed = {"Read", "Grep", "Glob", "WebFetch", "WebSearch"}
    if data["role"] not in READ_ONLY or readonly(command, tool) or control_read:
        allowed.update(("Bash", "PowerShell"))
    if data["role"] in ("dev-small", "dev-medium"):
        allowed.update(("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "ApplyPatch"))
    if tool not in allowed:
        return "tool is outside the assigned worker role"
    targets = questions.tool_targets(payload)
    if command:
        paths, trees = questions.shell_targets(command, tool, payload["cwd"], mission)
        targets.extend(paths + trees)
    for target in targets:
        path = os.path.realpath(os.path.join(payload["cwd"], target))
        if not inside_real_root(data["cwd"], path):
            return "worker write escaped its workspace"
        relative = Path(os.path.relpath(path, data["cwd"]))
        if any(part.casefold() in (".git", ".chef", ".claude", ".codex", ".grok", ".hermes")
               for part in relative.parts):
            return "worker cannot change executable policy or repository metadata"
        state = data["owner"]["state"]
        if same_path(path, state) or inside_real_root(state, path) or inside_real_root(path, state):
            return "worker cannot change parent mission state"
        files = data["owner"].get("files", [])
        if files and not any(same_path(path, os.path.join(data["cwd"], item)) for item in files):
            return "worker write is outside the assigned files"
    legacy = {**data, "guard": data.get("guard")}
    return project_denial({**payload, "han_hermes_worker": legacy})
