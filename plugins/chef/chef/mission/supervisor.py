"""Task Queue & Dispatch (작업 대기열·배정): bounded dispatch through existing slots.

Configuration schema 1 has projects {name: {path, slots, runtimes, resources}}
and budgets {maxRuns, maxConcurrent, maxSeconds, launchTimeoutSeconds}.
All CLI callers share <CHEF_SUPERVISOR_HOME>/queue.json; the default home is
~/.chef/supervisor. Use one shared home across runtimes. The Python helpers have
injected process boundaries for tests; invoke the CLI for production dispatch.
The queue is bound to that exact configuration; changing policy needs a new queue.
Uncertain launches retain their reservation until an operator resolves them outside
this CLI. This module intentionally offers no retry, takeover or approve command.
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from chef.infrastructure.storage import atomic_write_json
from chef.infrastructure.paths import inside_real_root, same_path

#!/usr/bin/env python3
from chef.locations import PLUGIN_ROOT
from chef.mission import state as mission_state
from chef.slots import commands as slots

guard = mission_state.mission_guard
verification = mission_state.verification
RUNTIMES = ("claude", "codex", "grok", "hermes")
HOLDING = {"reserved", "launched", "running", "needs-operator"}
STATUSES = HOLDING | {"pending", "completed"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SLOTS_SCRIPT = PLUGIN_ROOT / "skills/slots/scripts/slots.py"


class SupervisorError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def authority_path():
    """One durable queue authority closes cross-queue launch/ACTIVE races."""
    home = os.environ.get("CHEF_SUPERVISOR_HOME") or str(Path.home() / ".chef" / "supervisor")
    if not Path(home).is_absolute():
        raise SupervisorError("CHEF_SUPERVISOR_HOME must be an absolute shared private directory")
    return (Path(home) / "queue.json").resolve()


def valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def atomic_write(path, value):
    atomic_write_json(path, value, durable=True, create_parents=True, newline=None)


def names(values, label):
    if (
        not isinstance(values, list)
        or not all(isinstance(v, str) and NAME.fullmatch(v) for v in values)
        or len(set(values)) != len(values)
    ):
        raise SupervisorError(f"{label}: expected unique safe names")
    return values


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict) or type(config.get("schema")) is not int or config.get("schema") != 1:
        raise SupervisorError("config schema must be 1")
    projects, budgets = config.get("projects"), config.get("budgets")
    if not isinstance(projects, dict) or not projects or not isinstance(budgets, dict):
        raise SupervisorError("explicit projects and budgets required")
    names(list(projects), "projects")
    for project in projects.values():
        if not isinstance(project, dict) or set(project) != {"path", "slots", "runtimes", "resources"}:
            raise SupervisorError("project requires only path, slots, runtimes, resources")
        path_value = project["path"]
        if (
            not isinstance(path_value, str)
            or not Path(path_value).is_absolute()
            or not Path(path_value).is_dir()
        ):
            raise SupervisorError("registered project path must be an existing absolute directory")
        names(project["slots"], "slots")
        names(project["resources"], "resources")
        names(project["runtimes"], "runtimes")
        if (
            not project["slots"]
            or not project["runtimes"]
            or any(r not in RUNTIMES for r in project["runtimes"])
        ):
            raise SupervisorError("registered slots and supported runtimes required")
    for key in ("maxRuns", "maxConcurrent", "maxSeconds", "launchTimeoutSeconds"):
        if type(budgets.get(key)) is not int or budgets[key] < 1:
            raise SupervisorError(f"budgets.{key} must be a positive integer")
    return config


def validate_state(state, config):
    if (
        not isinstance(state, dict)
        or type(state.get("schema")) is not int
        or state.get("schema") != 1
        or state.get("configHash") != digest(config)
        or not isinstance(state.get("tasks"), list)
        or type(state.get("runs")) is not int
        or state["runs"] < 0
        or not valid_time(state.get("createdAt"))
    ):
        raise SupervisorError("queue schema/configuration mismatch or corrupt queue")
    seen = set()
    for task in state["tasks"]:
        if not isinstance(task, dict):
            raise SupervisorError("corrupt task")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not NAME.fullmatch(task_id) or task_id in seen:
            raise SupervisorError("invalid or duplicate task id")
        seen.add(task_id)
        project = config["projects"].get(task.get("project"))
        if (
            not project
            or task.get("runtime") not in project["runtimes"]
            or task.get("status") not in STATUSES
            or type(task.get("priority")) is not int
        ):
            raise SupervisorError("invalid task registration, state or priority")
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal.strip() or any(c in goal for c in ('"', "\n", "\r", "\x00")):
            raise SupervisorError("goal must be nonempty one-line slot task without quotes")
        names(task.get("after"), "dependencies")
        if task["status"] in HOLDING:
            assignment = task.get("assignment")
            if (
                not isinstance(assignment, dict)
                or assignment.get("slot") not in project["slots"]
                or not isinstance(assignment.get("path"), str)
                or not Path(assignment["path"]).is_absolute()
                or assignment.get("resources") != project["resources"]
                or not isinstance(assignment.get("knownMissions"), list)
                or not all(isinstance(item, str) for item in assignment["knownMissions"])
                or not valid_time(task.get("reservedAt"))
            ):
                raise SupervisorError("holding task lacks valid reservation")
        if task.get("mission") and not guard.valid_mission_id(task["mission"]):
            raise SupervisorError("invalid observed mission id")
    if state["runs"] < sum(task["status"] != "pending" for task in state["tasks"]):
        raise SupervisorError("queue launch count is inconsistent with reservations")
    graph = {task["id"]: task["after"] for task in state["tasks"]}
    visited, visiting = set(), set()

    def visit(node):
        if node not in graph:
            raise SupervisorError(f"unknown dependency: {node}")
        if node in visiting:
            raise SupervisorError("dependency cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    return state


def load_state(path, config, now=None):
    if Path(path).exists():
        return validate_state(read_json(path), config)
    return {
        "schema": 1,
        "configHash": digest(config),
        "createdAt": time.time() if now is None else now,
        "runs": 0,
        "tasks": [],
    }


def enqueue(state, config, task_id, project, goal, runtime=None, priority=0, after=()):
    registered = config["projects"].get(project)
    if not registered:
        raise SupervisorError("unregistered project")
    task = {
        "id": task_id,
        "project": project,
        "goal": goal,
        "runtime": runtime or registered["runtimes"][0],
        "priority": priority,
        "after": list(after),
        "status": "pending",
    }
    candidate = {**state, "tasks": [*state["tasks"], task]}
    validate_state(candidate, config)
    state["tasks"].append(task)
    return task


def runtime_ready(runtime, project=None):
    if runtime == "hermes":
        return False  # Native current-session entry is not an unattended launcher.
    from chef.runtimes.launch import launcher_problem
    from types import SimpleNamespace
    try:
        if project:
            located = slots.locate(project["path"], runtime)
            config = slots.Config(located, slots.read_adapter(located.adapter_path, runtime), runtime)
        else:
            config = SimpleNamespace(runtime=runtime, terminal="auto")
        return not launcher_problem(config, unattended=True)
    except Exception:
        return False


def observe(project, slot, runtime, mission=""):
    located = slots.locate(project["path"], runtime)
    adapter = slots.read_adapter(located.adapter_path, runtime)
    config = slots.Config(located, adapter, runtime)
    config.check_name(slot)
    active = slots.active_of(config, slot)
    state_dir = Path(config.slot_state_dir(slot))
    result = {
        "path": str(Path(config.slot_path(slot)).resolve()),
        "ready": not slots.idle_refusal(config, slot, False),
        "active": active.id,
        "session": active.session,
        "corrupt": active.corrupt,
        "stale": bool(
            active.id
            and not active.corrupt
            and guard.staleness(str(state_dir), active, guard.stale_hours_of(adapter)).stale
        ),
        "missions": sorted(p.name for p in state_dir.iterdir() if p.is_dir()) if state_dir.is_dir() else [],
        "completed": False,
        "interrupted": False,
    }
    if mission:
        folder = state_dir / mission
        fields = mission_state.header_fields(mission_state.read_text(folder / "mission.md").splitlines())
        result["interrupted"] = fields.get("상태") == "중단"
        if fields.get("상태") == "닫힘" and fields.get("확인 C", "").startswith("승인") and not active.id:
            mission_state.require_question_integrity(folder)
            units = mission_state.read_table(folder / "units.tsv", mission_state.UNIT_HEADER)
            completed = bool(units) and all(unit["status"] == "done" for unit in units)
            for unit in units:
                directory = folder / "verification"
                receipt = verification.read_json(directory / f"{unit['id']}.latest.json")["receipt"]
                review = verification.read_json(directory / f"{unit['id']}.review.latest.json")["review"]
                verification.validate_receipt(
                    result["path"],
                    folder,
                    unit["id"],
                    verification.read_json(receipt),
                    settings=adapter.get("verify") or {},
                )
                verification.validate_review(folder, unit["id"], receipt, review)
            result["completed"] = completed
    return result


def overlaps(first, second):
    return same_path(first, second) or inside_real_root(first, second) or inside_real_root(second, first)


def conflicts(assignment, occupied):
    return any(
        overlaps(assignment["path"], other["path"])
        or bool(set(assignment["resources"]) & set(other["resources"]))
        for other in occupied
    )


def plan(state, config, observer=observe, readiness=None, now=None):
    now = time.time() if now is None else now
    budgets = config["budgets"]
    holding = [task for task in state["tasks"] if task["status"] in HOLDING]
    occupied = [task["assignment"] for task in holding]
    remaining = min(budgets["maxConcurrent"] - len(holding), budgets["maxRuns"] - state["runs"])
    if now - state["createdAt"] >= budgets["maxSeconds"]:
        return {"dispatch": [], "blocked": {"budget": "elapsed time budget exhausted"}}
    decisions, blocked = [], {}
    done = {task["id"] for task in state["tasks"] if task["status"] == "completed"}
    pending = sorted((t for t in state["tasks"] if t["status"] == "pending"), key=lambda t: -t["priority"])
    for task in pending:
        reason = "run/concurrency budget exhausted"
        if any(dependency not in done for dependency in task["after"]):
            blocked[task["id"]] = "dependencies incomplete"
            continue
        if not (readiness(task["runtime"]) if readiness else runtime_ready(task["runtime"], config["projects"][task["project"]])):
            blocked[task["id"]] = "runtime or terminal executable unavailable"
            continue
        if remaining > 0:
            project = config["projects"][task["project"]]
            for slot in project["slots"]:
                try:
                    view = observer(project, slot, task["runtime"])
                    assignment = {
                        "slot": slot,
                        "path": view["path"],
                        "resources": project["resources"],
                        "knownMissions": view.get("missions", []),
                    }
                    if (
                        view["corrupt"]
                        or view["active"]
                        or not view["ready"]
                        or conflicts(assignment, occupied)
                    ):
                        reason = "slots busy, unready, corrupt or resource/path collision"
                        continue
                except Exception as error:
                    reason = f"slot observation failed: {error}"
                    continue
                decisions.append({"id": task["id"], "assignment": assignment})
                occupied.append(assignment)
                remaining -= 1
                break
            else:
                blocked[task["id"]] = reason
                continue
            continue
        blocked[task["id"]] = reason
    return {"dispatch": decisions, "blocked": blocked}


def launch(task, project):
    # No arbitrary command templates, shell, resume, refresh or editor options.
    command = [
        sys.executable,
        str(SLOTS_SCRIPT),
        "--project",
        project["path"],
        "--runtime",
        task["runtime"],
        "run",
        task["assignment"]["slot"],
        "--",
        task["goal"],
    ]
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
        env=slots.launch_env(),
    ).returncode


def dispatch(
    state, config, save, execute=False, observer=observe, readiness=None, launcher=launch, now=None
):
    clock = time.time if now is None else lambda: now
    decision = plan(state, config, observer, readiness, clock())
    if not execute:
        return decision
    decision["executed"] = []
    for selected in decision["dispatch"]:
        current = plan(state, config, observer, readiness, clock())
        available = next((item for item in current["dispatch"] if item["id"] == selected["id"]), None)
        if available is None:
            decision["blocked"][selected["id"]] = current["blocked"].get(
                selected["id"], "no longer dispatchable"
            )
            continue
        task = next(t for t in state["tasks"] if t["id"] == selected["id"])
        # Each launch is reserved durably before crossing the process boundary.
        task.update(status="reserved", assignment=available["assignment"], reservedAt=clock())
        state["runs"] += 1
        save(state)
        try:
            code = launcher(task, config["projects"][task["project"]])
            task["status"] = "launched" if code == 0 else "needs-operator"
            task["reason"] = (
                "launch requested; awaiting ACTIVE"
                if code == 0
                else f"launcher exited {code}; inspect before retry"
            )
        except Exception as error:
            task.update(status="needs-operator", reason=f"uncertain launch: {error}")
        save(state)
        decision["executed"].append({"id": task["id"], "status": task["status"], "reason": task["reason"]})
    return decision


def reconcile(state, config, observer=observe, now=None):
    now = time.time() if now is None else now
    for task in state["tasks"]:
        if task["status"] not in HOLDING or task["status"] == "needs-operator":
            continue
        if task["status"] == "reserved":
            task.update(
                status="needs-operator", reason="supervisor interrupted during launch; reservation retained"
            )
            continue
        try:
            view = observer(
                config["projects"][task["project"]],
                task["assignment"]["slot"],
                task["runtime"],
                task.get("mission", ""),
            )
            if view["path"] != task["assignment"]["path"] or view["corrupt"]:
                raise SupervisorError("slot path changed or ACTIVE corrupt")
            if view.get("stale") or view.get("interrupted"):
                raise SupervisorError("mission interrupted or stale; operator inspection required")
            active = view["active"]
            if active:
                if (
                    (task.get("mission") and active != task["mission"])
                    or active in task["assignment"]["knownMissions"]
                    or (task.get("session") and task["session"] != view["session"])
                    or not view["session"]
                ):
                    raise SupervisorError("unrelated ACTIVE; ownership not assumed")
                task.update(status="running", mission=active, session=view["session"])
            elif task.get("mission") and view["completed"]:
                task.update(
                    status="completed",
                    reason="observed ACTIVE mission closed with C approval and verified/reviewed units",
                )
            elif task.get("mission") or view.get("interrupted"):
                raise SupervisorError("mission no longer active without validated completion")
            elif now - task["reservedAt"] >= config["budgets"]["launchTimeoutSeconds"]:
                raise SupervisorError("launch not acknowledged by ACTIVE; inspect child session")
        except Exception as error:
            task.update(status="needs-operator", reason=str(error))
    return state


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("enqueue")
    add.add_argument("id")
    add.add_argument("--project", required=True)
    add.add_argument("--goal", required=True)
    add.add_argument("--runtime", choices=RUNTIMES)
    add.add_argument("--priority", type=int, default=0)
    add.add_argument("--after", nargs="*", default=[])
    sub.add_parser("status")
    sub.add_parser("plan")
    sub.add_parser("reconcile")
    sub.add_parser("dispatch").add_argument("--execute", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        path = Path(args.state).resolve()
        if path != authority_path():
            raise SupervisorError(
                f"all Supervisor callers must use the shared authority queue: {authority_path()}"
            )
        if path == Path(args.config).resolve():
            raise SupervisorError("queue and configuration must be different files")
        mutating = args.command in ("enqueue", "reconcile") or (args.command == "dispatch" and args.execute)
        if mutating:
            path.parent.mkdir(parents=True, exist_ok=True)
        import contextlib

        with (
            slots.FileLock(str(path) + ".lock", "supervisor queue", timeout=0)
            if mutating
            else contextlib.nullcontext()
        ):
            state = load_state(path, config)
            if args.command == "enqueue":
                result = enqueue(
                    state, config, args.id, args.project, args.goal, args.runtime, args.priority, args.after
                )
                atomic_write(path, state)
            elif args.command == "status":
                result = state
            elif args.command == "plan":
                result = plan(state, config)
            elif args.command == "dispatch":
                result = dispatch(state, config, lambda value: atomic_write(path, value), args.execute)
            else:
                result = reconcile(state, config)
                atomic_write(path, state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        SupervisorError,
        slots.UsageError,
        slots.LockBusy,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        print(f"supervisor: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
