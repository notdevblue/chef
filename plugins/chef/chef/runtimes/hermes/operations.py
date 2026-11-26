"""Native mission operations. Pre-tool hooks validate; handlers commit state."""

import os

from chef.cli import mission_entry
from chef.guards import mission
from chef.infrastructure.configuration import read_adapter
from chef.infrastructure.paths import same_path


def prepare(bridge, args, session, task):
    if (not isinstance(args, dict) or set(args) - {"action", "mission"}
            or args.get("action") not in ("open", "resume", "close", "status")):
        raise ValueError("chef_mission requires action and, for transitions, a mission ID")
    if bridge.is_worker(session):
        raise ValueError("only the parent session can operate a mission")
    action = args["action"]
    name = args.get("mission")
    if action == "status":
        if name is not None:
            raise ValueError("mission status takes no mission ID")
    elif not isinstance(name, str) or not mission.valid_mission_id(name):
        raise ValueError("a valid mission ID is required")
    cwd = bridge.cwd(session, task)
    payload = {"han_runtime": "hermes", "hook_event_name": "PreToolUse", "cwd": cwd,
               "session_id": session, "task_id": task, "tool_name": "Bash",
               "tool_input": {"command": f"echo han-mission {action} {name}"}}
    located = mission.locate_adapter(payload)
    if located is None:
        raise ValueError("no Chef adapter for this task; select its workspace first")
    adapter = read_adapter(located.adapter_path, "hermes")
    from chef.runtimes.hermes.plugin import configuration_problem

    problem = configuration_problem(located)
    if problem:
        raise ValueError(problem)
    state = os.path.realpath(mission.state_dir_of(located.project, adapter))
    active = mission.read_active(state)
    if active.corrupt:
        raise ValueError("ACTIVE is corrupt; inspect it before continuing")
    if action != "status":
        if not same_path(cwd, located.project):
            raise ValueError("operate the mission at the selected workspace root")
        if active.id and (active.session != session or active.runtime not in ("", "hermes")):
            raise ValueError("this mission belongs to another session or runtime")
        if action in ("close", "resume") and (not active.id or active.id != name):
            raise ValueError("this operation requires the matching owned ACTIVE mission")
        if action == "open" and active.id:
            raise ValueError("a mission is already active; use resume")
        if action in ("open", "resume"):
            mission_entry.guard_location(payload, "new" if action == "open" else "resume", name)
    return payload, located, adapter, state, active


def execute(bridge, args, session, task):
    payload, located, adapter, state, active = prepare(bridge, args, session, task)
    action = args["action"]
    if action == "status":
        return {"ok": True, "action": action, "workspace": located.project,
                "active": bool(active.id), "mission": active.id or None,
                "owned": bool(active.id and active.session == session and active.runtime in ("", "hermes"))}
    with bridge.lock:
        context = bridge.bindings.get((session, task), {})
    from chef.runtimes.hermes import external_slots

    if external_slots.configured(adapter) or context.get("external"):
        # This callback is now executed only inside the tool handler, after the
        # host has accepted every plugin's pre-tool check.
        directive = bridge.external_tool(context, "terminal", payload, located)
        if directive:
            raise ValueError(directive["message"])
    else:
        with mission_entry.active_lock(state):
            payload, located, adapter, checked_state, _ = prepare(bridge, args, session, task)
            if not same_path(checked_state, state):
                raise ValueError("the mission state location changed before commit")
            if action in ("open", "resume"):
                checked = mission_entry.guard_location(payload, "new" if action == "open" else "resume", args["mission"])
                if checked and (not same_path(checked["located"].project, located.project)
                        or not same_path(checked["located"].adapter_path, located.adapter_path)):
                    raise ValueError("mission entry receipt names another workspace")
            outcome = mission.Outcome()
            marker = mission.MARKER.fullmatch(payload["tool_input"]["command"])
            mission.handle_marker(marker, state, session, outcome, runtime="hermes")
            if outcome.deny:
                raise ValueError(outcome.deny)
        if action == "close" and context:
            bridge.restore_slot_bindings(session, context)
    return {"ok": True, "action": action, "mission": args["mission"]}
