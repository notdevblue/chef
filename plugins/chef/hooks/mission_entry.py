import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid

import mission_guard

CONFIG_FILE = "mission-entry.json"
RECEIPTS_DIR = "mission-entry-receipts"
RECEIPT_SECONDS = 600
RUNTIMES = ("claude", "codex", "grok")
MODES = ("new", "resume")
DEPLOY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EntryError(Exception):
    pass


def path_key(path):
    return os.path.normcase(os.path.realpath(path))


def inside(path, root):
    try:
        return os.path.commonpath([path_key(path), path_key(root)]) == path_key(root)
    except ValueError:
        return False


def absolute_path(value, label, directory=False):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise EntryError(f"{label} must be an absolute path")
    result = os.path.realpath(value)
    if directory:
        valid = os.path.isdir(result)
    else:
        valid = os.path.isfile(result)
    if not valid:
        raise EntryError(f"{label} does not exist: {result}")
    return result


def read_object(path):
    try:
        data = mission_guard.read_json_file(path)
    except (OSError, ValueError) as exc:
        raise EntryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EntryError(f"JSON object required: {path}")
    return data


def digest_file(path):
    try:
        with open(path, "rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise EntryError(f"cannot fingerprint {path}: {exc}") from exc


def digest_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def registration(cwd, runtime):
    cwd = os.path.realpath(cwd)
    directory = mission_guard.registry_dir(runtime)
    if not directory:
        return {}
    path = os.path.join(directory, CONFIG_FILE)
    if not os.path.isfile(path):
        return {}
    if inside(path, DEPLOY_ROOT):
        raise EntryError("mission entry registration must be outside the installed plugin")
    data = read_object(path)
    if type(data.get("schema")) is not int or data["schema"] != 1 or not isinstance(data.get("events"), list):
        raise EntryError("mission-entry.json requires schema 1 and events[]")
    matches = []
    for event in data["events"]:
        if not isinstance(event, dict) or not isinstance(event.get("roots"), list) or not event["roots"]:
            raise EntryError("each mission entry event requires roots[]")
        if not all(isinstance(root, str) and os.path.isabs(root) for root in event["roots"]):
            raise EntryError("mission entry roots must be absolute paths or path-part globs")
        if any(mission_guard.root_matches(folder, root) for folder in mission_guard.ancestors(cwd) for root in event["roots"]):
            matches.append(event)
    if not matches:
        return {}
    if len(matches) != 1:
        raise EntryError("multiple mission entry events match this working directory")
    event = matches[0]
    if not isinstance(event.get("id"), str) or not mission_guard.valid_mission_id(event["id"]):
        raise EntryError("mission entry event requires a valid id")
    if event.get("trusted") is not True:
        raise EntryError("mission entry event has not been explicitly trusted")
    allowed_roots = event.get("allowedRoots", event["roots"])
    if not isinstance(allowed_roots, list) or not allowed_roots or not all(isinstance(root, str) and os.path.isabs(root) for root in allowed_roots):
        raise EntryError("allowedRoots must be absolute paths or path-part globs")
    command = event.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
        raise EntryError("mission entry command must be a nonempty argv array")
    executable = absolute_path(command[0], "command[0]")
    if inside(executable, DEPLOY_ROOT):
        raise EntryError("external event executable must be outside the installed plugin")
    timeout = event.get("timeoutSeconds", 120)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise EntryError("timeoutSeconds must be an integer from 1 to 3600")
    watched = event.get("watchFiles")
    if not isinstance(watched, list) or not watched:
        raise EntryError("mission entry event requires watchFiles[]")
    files = {}
    for index, value in enumerate(watched):
        source = absolute_path(value, f"watchFiles[{index}]")
        files[path_key(source)] = digest_file(source)
    for arg in command[1:]:
        if not os.path.isabs(arg) and os.path.isfile(os.path.join(cwd, arg)):
            raise EntryError(f"command input files must use absolute paths: {arg}")
        if os.path.isabs(arg) and os.path.isfile(arg):
            source = os.path.realpath(arg)
            if inside(source, DEPLOY_ROOT):
                raise EntryError(f"external event command input must be outside the installed plugin: {arg}")
            if path_key(source) not in files:
                raise EntryError(f"command input file must appear in watchFiles: {arg}")
    executable_stat = os.stat(executable)
    return {"path": os.path.realpath(path), "event": event,
            "fingerprint": digest_json({"registration": digest_file(path), "executable": path_key(executable),
                                        "executableStat": [executable_stat.st_size, executable_stat.st_mtime_ns], "files": files})}


def validate_request(cwd, runtime, mode, session, mission):
    if runtime not in RUNTIMES or mode not in MODES:
        raise EntryError("unsupported runtime or mode")
    if not isinstance(session, str) or not session.strip() or any(char in session for char in "\t\r\n"):
        raise EntryError("a session id is required")
    if not isinstance(mission, str) or not mission_guard.valid_mission_id(mission):
        raise EntryError("a valid mission id is required")
    return absolute_path(cwd, "project", directory=True)


def find_location(cwd, runtime):
    saved = os.environ.pop("CLAUDE_PROJECT_DIR", "")
    try:
        return mission_guard.find_adapter({"cwd": cwd, "han_runtime": runtime})
    except mission_guard.AdapterNotFound:
        return mission_guard.Located("", "")
    finally:
        if saved:
            os.environ["CLAUDE_PROJECT_DIR"] = saved


def load_location(project, adapter, runtime):
    project = absolute_path(project, "project", directory=True)
    adapter = absolute_path(adapter, "adapter")
    problems = []
    data = mission_guard.load_adapter(adapter, problems, runtime)
    if problems:
        raise EntryError("; ".join(problems))
    mission_guard.state_dir_of(project, data)
    return mission_guard.Located(project, adapter), data


def is_slot(cwd, located, adapter, runtime):
    if not located.project or not isinstance(adapter.get("slots"), dict) or not adapter["slots"].get("enabled"):
        return False
    slots = slot_tools()
    try:
        config = slots.Config(located, adapter, runtime)
    except slots.UsageError as exc:
        raise EntryError(str(exc)) from exc
    return any(inside(cwd, config.slot_path(name)) for name in config.names)


def slot_tools():
    sys.path.insert(0, os.path.join(DEPLOY_ROOT, "skills", "slots", "scripts"))
    import slots

    return slots


def active_lock(state_dir):
    return slot_tools().FileLock(os.path.realpath(mission_guard.active_path_of(state_dir)) + ".entry.lock", "mission ACTIVE")


def check_resume_runtime(state_dir, active, runtime):
    recorded = active.runtime
    if not recorded:
        try:
            recorded = slot_tools().session_runtime(os.path.join(state_dir, active.id), active.session, strict=True)
        except Exception as exc:
            raise EntryError(f"registered resume runtime evidence could not be read: {exc}") from exc
    if not recorded:
        raise EntryError("registered resume cannot establish the existing runtime from ACTIVE or session evidence")
    if recorded != runtime:
        raise EntryError(f"registered resume must preserve the existing runtime: {recorded}")


def execute_event(selected, request):
    event = selected["event"]
    try:
        done = subprocess.run(event["command"], input=json.dumps(request, ensure_ascii=True), capture_output=True,
                              text=True, encoding="utf-8", cwd=request["cwd"], timeout=event.get("timeoutSeconds", 120), shell=False)
    except subprocess.TimeoutExpired as exc:
        raise EntryError("mission entry event timed out") from exc
    except (OSError, UnicodeError) as exc:
        raise EntryError(f"mission entry event could not run: {exc}") from exc
    if done.returncode:
        raise EntryError(f"mission entry event failed with exit code {done.returncode}: {done.stderr.strip()[:500]}")
    try:
        result = json.loads(done.stdout)
    except ValueError as exc:
        raise EntryError("mission entry event must return one JSON object on stdout") from exc
    if not isinstance(result, dict) or type(result.get("schema")) is not int or result["schema"] != 1 or result.get("action") not in ("continue", "delegate", "needs-input"):
        raise EntryError("invalid mission entry event result")
    fields = {"continue": {"schema", "action", "project", "adapter"}, "delegate": {"schema", "action", "project", "slot"},
              "needs-input": {"schema", "action", "message"}}
    if set(result) - fields[result["action"]]:
        raise EntryError("unknown mission entry result fields")
    if result["action"] == "needs-input":
        if not isinstance(result.get("message"), str) or not result["message"].strip():
            raise EntryError("needs-input requires a message")
    return result


def binding(cwd, runtime, mode, session, mission):
    return {"cwd": path_key(cwd), "runtime": runtime, "mode": mode, "sessionId": session, "missionId": mission}


def receipt_path(selected, key):
    path = os.path.join(os.path.dirname(selected["path"]), RECEIPTS_DIR, key["runtime"], digest_json(key) + ".json")
    if inside(path, DEPLOY_ROOT):
        raise EntryError("mission entry receipts must be outside the installed plugin")
    return path


def adapter_fingerprint(located):
    paths = [located.adapter_path, os.path.join(os.path.dirname(located.adapter_path), "local.json")]
    files = {}
    for path in paths:
        if os.path.isfile(path):
            files[path_key(path)] = digest_file(path)
        else:
            files[path_key(path)] = "absent"
    return digest_json(files)


def check_route(selected, project):
    roots = selected["event"].get("allowedRoots", selected["event"]["roots"])
    if not isinstance(roots, list) or not roots or not all(isinstance(root, str) and os.path.isabs(root) for root in roots):
        raise EntryError("allowedRoots must be absolute paths or path-part globs")
    if not any(mission_guard.root_matches(folder, root) for folder in mission_guard.ancestors(project) for root in roots):
        raise EntryError("mission entry route is outside the registered allowedRoots")


def write_receipt(selected, key, located, active):
    path = receipt_path(selected, key)
    data = {"schema": 1, "binding": key, "fingerprint": selected["fingerprint"], "createdAt": time.time(),
            "project": located.project, "adapter": located.adapter_path, "adapterFingerprint": adapter_fingerprint(located),
            "active": active._asdict()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def guard_location(payload, mode, mission):
    cwd = os.path.realpath(payload.get("cwd") or "")
    runtime = payload.get("han_runtime", "claude")
    selected = registration(cwd, runtime)
    if not selected:
        return {}
    session = payload.get("session_id") or ""
    cwd = validate_request(cwd, runtime, mode, session, mission)
    key = binding(cwd, runtime, mode, session, mission)
    data = read_object(receipt_path(selected, key))
    created = data.get("createdAt", 0)
    if type(created) not in (int, float):
        raise EntryError("invalid mission entry receipt timestamp")
    age = time.time() - created
    if type(data.get("schema")) is not int or data["schema"] != 1 or data.get("binding") != key or not 0 <= age <= RECEIPT_SECONDS:
        raise EntryError("mission entry receipt is missing, stale or bound to another invocation; run mission_entry.py first")
    if data.get("fingerprint") != selected["fingerprint"]:
        raise EntryError("mission entry registration or external source changed; run mission_entry.py again")
    located, adapter = load_location(data.get("project", ""), data.get("adapter", ""), runtime)
    check_route(selected, located.project)
    discovered = find_location(cwd, runtime)
    if path_key(discovered.project) != path_key(located.project) or path_key(discovered.adapter_path) != path_key(located.adapter_path):
        raise EntryError("mission entry route changed; run mission_entry.py again")
    if not inside(cwd, located.project) or data.get("adapterFingerprint") != adapter_fingerprint(located):
        raise EntryError("mission entry adapter changed; run mission_entry.py again")
    active = mission_guard.read_active(mission_guard.state_dir_of(located.project, adapter))
    if data.get("active") != active._asdict():
        raise EntryError("mission entry ACTIVE changed; run mission_entry.py again")
    if mode == "resume" and active.session != session:
        raise EntryError("registered resume must preserve the ACTIVE session")
    if active.id:
        check_resume_runtime(mission_guard.state_dir_of(located.project, adapter), active, runtime)
    return {"located": located, "adapter": adapter}


def delegate(result, request, selected):
    if request["isSlot"]:
        raise EntryError("a slot entry cannot delegate again")
    project = absolute_path(result.get("project"), "delegate.project", directory=True)
    check_route(selected, project)
    located = find_location(project, request["runtime"])
    if not located.project:
        raise EntryError("delegate project has no discoverable adapter")
    located, adapter = load_location(located.project, located.adapter_path, request["runtime"])
    slots = slot_tools()
    try:
        config = slots.Config(located, adapter, request["runtime"])
    except slots.UsageError as exc:
        raise EntryError(str(exc)) from exc
    name = result.get("slot")
    if not isinstance(name, str) or name not in config.names:
        raise EntryError("delegate.slot must name a registered slot")
    check_route(selected, config.slot_path(name))
    if inside(request["cwd"], config.slot_path(name)):
        raise EntryError("cannot delegate back to the current slot")
    argv = ["--project", located.project, "run", name]
    if request["mode"] == "resume":
        argv.append("--resume")
        active = mission_guard.read_active(config.slot_state_dir(name))
        if active.id != request["missionId"]:
            raise EntryError("delegate resume mission does not match the slot ACTIVE")
        check_resume_runtime(config.slot_state_dir(name), active, request["runtime"])
    else:
        if not request["task"].strip():
            raise EntryError("delegating a new mission requires --task")
        argv.append(request["task"])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = slots.main(argv, runtime=request["runtime"])
    if output.getvalue():
        print(output.getvalue(), file=sys.stderr, end="")
    if code:
        raise EntryError(f"slot delegation failed with exit code {code}")
    return {"schema": 1, "action": "delegate", "project": located.project, "slot": name, "launched": True}


def enter(cwd, runtime, mode, session, mission, task=""):
    cwd = validate_request(cwd, runtime, mode, session, mission)
    selected = registration(cwd, runtime)
    if not selected:
        return enter_locked(cwd, runtime, mode, session, mission, task, selected)
    key = binding(cwd, runtime, mode, session, mission)
    previous = receipt_path(selected, key)
    with slot_tools().FileLock(previous + ".lock", "mission entry", selected["event"].get("timeoutSeconds", 120) + 30):
        if os.path.exists(previous):
            os.unlink(previous)
        current = registration(cwd, runtime)
        if current.get("fingerprint") != selected["fingerprint"]:
            raise EntryError("mission entry registration changed while waiting for the entry lock")
        return enter_locked(cwd, runtime, mode, session, mission, task, current)


def enter_locked(cwd, runtime, mode, session, mission, task, selected):
    located = find_location(cwd, runtime)
    adapter = {}
    previous_active = mission_guard.NO_ACTIVE
    previous_adapter = ""
    previous_state = ""
    if located.project:
        located, adapter = load_location(located.project, located.adapter_path, runtime)
        previous_state = mission_guard.state_dir_of(located.project, adapter)
        previous_active = mission_guard.read_active(previous_state)
        previous_adapter = adapter_fingerprint(located)
        if previous_active.corrupt:
            raise EntryError("ACTIVE is corrupt")
        if mode == "new" and previous_active.id and (previous_active.id != mission or previous_active.session != session):
            raise EntryError("an existing ACTIVE must be resumed or closed explicitly")
        if selected and mode == "resume" and previous_active.id and previous_active.session != session:
            raise EntryError("registered resume must preserve the ACTIVE session")
        if selected and previous_active.id:
            check_resume_runtime(previous_state, previous_active, runtime)
    request = {"schema": 1, "event": "beforeMission", "runtime": runtime, "cwd": cwd, "mode": mode,
               "sessionId": session, "missionId": mission, "task": task, "isSlot": is_slot(cwd, located, adapter, runtime)}
    result = {"schema": 1, "action": "continue"}
    if selected:
        result = execute_event(selected, request)
        if registration(cwd, runtime).get("fingerprint") != selected["fingerprint"]:
            raise EntryError("mission entry registration or external source changed while running")
        if previous_state and mission_guard.read_active(previous_state) != previous_active:
            raise EntryError("mission entry event must not change ACTIVE")
        if previous_active.id and adapter_fingerprint(located) != previous_adapter:
            raise EntryError("entry must preserve the existing adapter binding")
    if result["action"] == "needs-input":
        return result
    if result["action"] == "delegate":
        if previous_active.id:
            raise EntryError("an active mission in the current project cannot be delegated")
        current = find_location(cwd, runtime)
        if current.project:
            current, current_adapter = load_location(current.project, current.adapter_path, runtime)
            if is_slot(cwd, current, current_adapter, runtime):
                raise EntryError("a slot entry cannot delegate again")
        return delegate(result, request, selected)
    discovered = find_location(cwd, runtime)
    if not discovered.project:
        raise EntryError("no discoverable adapter after mission entry; register or create the adapter explicitly")
    project = result.get("project", discovered.project)
    adapter_path = result.get("adapter", discovered.adapter_path)
    located, adapter = load_location(project, adapter_path, runtime)
    if previous_active.id and (path_key(previous_state) != path_key(mission_guard.state_dir_of(located.project, adapter))
                               or adapter_fingerprint(located) != previous_adapter):
        raise EntryError("entry must preserve the existing adapter binding")
    if selected:
        check_route(selected, located.project)
    if not inside(cwd, located.project):
        raise EntryError("continue.project must contain the invocation working directory")
    if discovered.project and (path_key(discovered.project) != path_key(located.project) or path_key(discovered.adapter_path) != path_key(located.adapter_path)):
        raise EntryError("continue route disagrees with adapter discovery")
    active = mission_guard.read_active(mission_guard.state_dir_of(located.project, adapter))
    if active.corrupt:
        raise EntryError("ACTIVE is corrupt")
    if mode == "new":
        if active.id and (active.id != mission or active.session != session):
            raise EntryError("an existing ACTIVE must be resumed or closed explicitly")
        if selected and active.id:
            check_resume_runtime(mission_guard.state_dir_of(located.project, adapter), active, runtime)
    else:
        if active.id != mission:
            raise EntryError("resume mission must match ACTIVE")
        if selected and active.session != session:
            raise EntryError("registered resume must preserve the ACTIVE session")
        if selected:
            check_resume_runtime(mission_guard.state_dir_of(located.project, adapter), active, runtime)
    response = {"schema": 1, "action": "continue", "project": located.project, "adapter": located.adapter_path}
    if selected:
        response["receipt"] = write_receipt(selected, binding(cwd, runtime, mode, session, mission), located, active)
    return response


def main(argv=None, runtime="claude"):
    parser = argparse.ArgumentParser(description="Run the trusted mission entry event before opening or resuming a mission")
    parser.add_argument("--runtime", choices=RUNTIMES, default=runtime)
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--mission", required=True)
    parser.add_argument("--task", default="")
    args = parser.parse_args(argv)
    try:
        result = enter(args.project, args.runtime, args.mode, args.session, args.mission, args.task)
        print(json.dumps(result, ensure_ascii=True))
        if result["action"] == "needs-input":
            return 3
        return 0
    except (EntryError, mission_guard.GuardOff, OSError, ValueError, RuntimeError) as exc:
        print(f"mission entry failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
