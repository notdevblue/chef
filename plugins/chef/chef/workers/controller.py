"""Owned, idempotent cross-runtime work with durable detached execution."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from chef.guards import mission
from chef.infrastructure import configuration, paths, user_config
from chef.infrastructure.fingerprints import implementation_digest
from chef.infrastructure.locking import FileLock
from chef.infrastructure.process import child_env
from chef.infrastructure.python import python_environment
from chef.infrastructure.storage import atomic_write_json
from chef.locations import PLUGIN_ROOT
from chef.mission import unit_contracts
from chef.mission.contracts import StateError, UNIT_HEADER, UNITS_FILE
from chef.mission.storage import read_table, require_unit
from chef.workers import backends
from chef.workers.routing import role_name
from chef.workers.store import Store, request_identity
from chef.verification.execution import process_owner_alive, process_start

RUNTIMES = ("claude", "codex", "grok")
MODES = ("auto", "native", *RUNTIMES)
READ_ONLY = {"research", "researcher", "review", "reviewSmall", "audit", "code-reviewer", "reviewer-small"}
ROLE_KEYS = {"researcher": "research", "dev-small": "implementSmall", "dev-medium": "implement",
             "verifier": "verify", "code-reviewer": "review", "reviewer-small": "reviewSmall"}
ACTIVE_STATUSES = {"reserved", "running", "needs-inspection"}
MAX_REQUEST = 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_REQUEST + 1)
    if len(data) > MAX_REQUEST:
        raise ValueError("worker JSON exceeds 1 MiB")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("worker JSON must be an object")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
        raise ValueError(f"{label} must be a nonempty single-line string")
    return value


def string_list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return list(dict.fromkeys(text(item, label) for item in value))


def project_paths(project, values, label):
    result = []
    for value in string_list(values, label):
        absolute = Path(os.path.normpath(os.path.abspath(os.path.join(project, value))))
        if not paths.inside_real_root(project, absolute):
            raise ValueError(f"{label} must stay inside the project")
        relative = absolute.relative_to(Path(project))
        current = Path(project)
        for part in relative.parts:
            current /= part
            if paths.is_link(current):
                raise ValueError(f"{label} cannot traverse links")
        if absolute.exists() and not absolute.is_file():
            raise ValueError(f"{label} accepts exact files only")
        result.append(relative.as_posix())
    return list(dict.fromkeys(result))


def snapshot(project, values):
    result = {}
    for name in project_paths(project, values, "snapshot"):
        path = Path(project, name)
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def ownership(request):
    runtime = request.get("ownerRuntime")
    if runtime not in RUNTIMES:
        raise ValueError("ownerRuntime must be claude, codex or grok")
    project = request.get("project")
    if not isinstance(project, str) or not Path(project).is_absolute() or not Path(project).is_dir():
        raise ValueError("project must be an existing absolute directory")
    located = configuration.locate(project, runtime)
    if not paths.same_path(located.project, project):
        raise ValueError("worker project must be the registered project root")
    adapter = configuration.read_adapter(located.adapter_path, runtime)
    state = os.path.realpath(mission.state_dir_of(located.project, adapter))
    active = mission.read_active(state)
    if (active.corrupt or not mission.valid_mission_id(active.id) or active.id != request.get("mission")
            or active.session != request.get("ownerSession")
            or active.runtime not in ("", runtime)):
        raise ValueError("worker request does not own the ACTIVE mission")
    if not (Path(state) / active.id / "mission.md").is_file():
        raise ValueError("initialize the owned mission before dispatch")
    try:
        row = require_unit(read_table(Path(state) / active.id / UNITS_FILE, UNIT_HEADER), request["unit"])
    except StateError as error:
        raise ValueError(str(error)) from error
    binding = unit_contracts.identity(row)
    if request.get("unitContract") and request["unitContract"] != binding:
        raise ValueError("worker unit brief changed")
    return adapter, {"runtime": runtime, "session": active.session,
                     "mission": active.id, "state": state, "unitContract": binding}


def routing_mode(request, settings):
    if "mode" in request:
        mode = request["mode"]
    elif "CHEF_WORKERS_MODE" in os.environ:
        mode = os.environ["CHEF_WORKERS_MODE"]
    elif "mode" in settings:
        mode = settings["mode"]
    else:
        workers = user_config.read().get("workers", {})
        if not isinstance(workers, dict):
            raise ValueError("global workers must be an object")
        mode = workers.get("mode", "auto")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("worker mode must be auto, native, claude, codex or grok")
    return mode


def prepare(value):
    if not isinstance(value, dict):
        raise ValueError("worker request must be an object")
    request = dict(value)
    for key in ("requestId", "unit", "role", "ownerSession", "mission"):
        text(request.get(key), key)
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ValueError("prompt must be nonempty text")
    timeout = request.get("timeout", 600)
    if type(timeout) is not int or not 10 <= timeout <= 3600:
        raise ValueError("timeout must be 10..3600 seconds")
    request["timeout"] = timeout
    if "independent" in request and type(request["independent"]) is not bool:
        raise ValueError("independent must be a boolean")
    if request.get("previous") is not None:
        text(request["previous"], "previous job key")
    project = request.get("project")
    if not isinstance(project, str) or not Path(project).is_absolute():
        raise ValueError("project must be an absolute directory")
    request["project"] = os.path.realpath(project)
    requested_role = request["role"].removeprefix("chef:")
    request["role"] = role_name(request["role"])
    adapter, owner = ownership(request)
    settings = adapter.get("workers", {})
    if not isinstance(settings, dict):
        raise ValueError("workers must be an object")
    mode = routing_mode(request, settings)
    request["mode"] = mode
    request["allowedRuntimes"] = list(RUNTIMES) if mode == "auto" else [owner["runtime"] if mode == "native" else mode]
    request["unitContract"] = owner["unitContract"]
    role_key = requested_role if requested_role in ("audit", "research", "implement", "implementSmall", "verify", "review", "reviewSmall") else ROLE_KEYS[request["role"]]
    roles = adapter.get("roles", {})
    request["actualRole"] = text(roles.get(role_key, roles.get("review") if role_key == "audit" else None)
                                  or "chef:" + request["role"], "actual role")
    project = request["project"]
    request["files"] = project_paths(project, request.get("files", []), "files")
    request["inputPaths"] = project_paths(project, request.get("inputPaths", []), "inputPaths")
    role = request["role"].removeprefix("chef:")
    if role not in READ_ONLY and not request["files"]:
        raise ValueError("write and verification workers require an explicit files scope")
    for key in ("requiredTools", "hostTools", "hostRoles"):
        request[key] = string_list(request.get(key, []), key)
    for name in request["files"]:
        target = Path(project, name)
        if paths.same_path(target, owner["state"]) or paths.inside_real_root(owner["state"], target):
            raise ValueError("worker files cannot include mission control state")
        if any(part.casefold() in (".git", ".claude", ".codex", ".grok", ".chef") for part in Path(name).parts):
            raise ValueError("worker files cannot include executable policy or repository metadata")
    request["before"] = snapshot(project, request["inputPaths"])
    request["beforeFiles"] = snapshot(project, request["files"])
    request["implementation"] = implementation_digest()
    request["adapterDigest"] = digest(adapter)
    request["inputDigest"] = digest({"inputs": request["before"], "files": request["beforeFiles"],
                                     "unit": request["unitContract"], "adapter": request["adapterDigest"],
                                     "implementation": request["implementation"]})
    intent = {key: item for key, item in request.items()
              if key not in ("before", "beforeFiles", "inputDigest", "ownerRuntime", "ownerSession", "ownerPid")}
    request["submissionIntent"] = digest(intent)
    request["condition"] = digest({"project": project, "condition": request.get("condition", ""),
                                    "adapter": request["adapterDigest"], "implementation": request["implementation"]})
    return request, adapter, owner


def effective_roles(adapter, request):
    return {**adapter.get("roles", {}), ROLE_KEYS[request["role"]]: request["actualRole"]}


def available_candidates(adapter, request, owner):
    allowed = request.get("allowedRuntimes", list(RUNTIMES))
    candidates = backends.candidates({**adapter, "roles": effective_roles(adapter, request)},
                                    request["project"], request["role"],
                                    native=owner["runtime"], host_tools=request["hostTools"],
                                    allowed_runtimes=allowed)
    candidates = [candidate for candidate in candidates if candidate["runtime"] in allowed]
    for candidate in candidates:
        candidate.setdefault("roles", [request["role"]])
        candidate.setdefault("tools", candidate.get("capabilities", []))
        if candidate["runtime"] == owner["runtime"]:
            if request["actualRole"] not in request["hostRoles"]:
                candidate.update(available=False, error="native role is not observed in the host role registry")
            elif not candidate.get("available") and request["actualRole"] not in {"chef:" + name for name in ROLE_KEYS}:
                candidate.update(available=True, tools=request["hostTools"],
                                 model="unknown", effort="unknown", policy="")
    return candidates


def attempt_directory(store, record):
    directory = Path(store.root) / "attempts" / record["key"] / record["token"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def overlaps(project, first, second):
    def identity(name):
        path = Path(project, name)
        try:
            value = path.stat()
            return ("file", value.st_dev, value.st_ino)
        except FileNotFoundError:
            return ("path", paths.canonical_path(path))
    return bool({identity(name) for name in first} & {identity(name) for name in second})


def check_conflicts(store, request):
    key, _ = request_identity(request)
    writes = request["files"] if request["role"].removeprefix("chef:") not in READ_ONLY else []
    reads = request["inputPaths"]
    for record in store.list(metadata=True):
        other = record.get("request", {})
        if record.get("status") not in ACTIVE_STATUSES or not paths.same_path(other.get("project"), request["project"]):
            continue
        if record["key"] == key:
            continue
        other_writes = other.get("files", []) if other.get("role", "").removeprefix("chef:") not in READ_ONLY else []
        if overlaps(request["project"], writes, other_writes + other.get("inputPaths", [])) or overlaps(request["project"], reads, other_writes):
            raise ValueError("worker file scope conflicts with an existing job; inspect or finish it first")


def existing_submission(store, request, submission):
    if not submission.exists():
        return None
    saved = read_json(submission)
    if saved.get("intent") != request["submissionIntent"]:
        raise ValueError("requestId was already submitted with different intent; use a new requestId")
    record = require_record(store, saved["key"])
    if any(record["request"].get(name) != request[name] for name in ("ownerRuntime", "ownerSession")):
        raise ValueError("recover the worker owner before retrieving this submission")
    native = (attempt_directory(store, record) / "native.json").exists()
    return {**record, "created": False, "native": native, "actualRole": record["request"]["actualRole"],
            "brief": record["request"]["prompt"] if native else None}


def dispatch(value, *, store=None, launch=None):
    store = store or Store()
    request, adapter, owner = prepare(value)
    workspace = os.stat(request["project"])
    identity = digest({"workspace": [workspace.st_dev, workspace.st_ino],
                       "mission": request["mission"], "requestId": request["requestId"]})
    submission = Path(store.root) / "submissions" / (identity + ".json")
    gate = str(Path(store.root) / "admission.lock")
    with FileLock(gate, "worker admission", timeout=10):
        existing = existing_submission(store, request, submission)
        if existing is not None:
            return existing
    # Authentication may involve slow local credential helpers. Keep these
    # probes outside shared admission so unrelated sessions can still dispatch.
    candidates = available_candidates(adapter, request, owner)
    previous = None
    previous_key = request.get("previous")
    if previous_key and not request.get("independent", False):
        prior = require_record(store, previous_key)
        for name in ("project", "mission", "unit", "role", "ownerRuntime", "ownerSession"):
            if prior["request"].get(name) != request[name]:
                raise ValueError("previous worker does not belong to this owner, unit and role")
        runtime = prior["route"]["runtime"]
        candidate = next((item for item in candidates if item["runtime"] == runtime and item.get("available")), None)
        session = prior.get("result", {}).get("session_id")
        if candidate and prior["route"].get("policy", "") == candidate.get("policy", "") and session:
            request["resumeSession"] = text(session, "previous session")
            previous = {"runtime": runtime}
    with FileLock(gate, "worker admission", timeout=10):
        existing = existing_submission(store, request, submission)
        if existing is not None:
            return existing
        current_adapter, _ = ownership(request)
        if digest(current_adapter) != request["adapterDigest"]:
            raise ValueError("worker adapter changed during admission")
        if (snapshot(request["project"], request["inputPaths"]) != request["before"]
                or snapshot(request["project"], request["files"]) != request["beforeFiles"]):
            raise ValueError("worker files changed during admission")
        check_conflicts(store, request)
        record = store.reserve(request, candidates, previous=previous,
                               independent=bool(request.get("independent", False)))
        directory = attempt_directory(store, record)
        native = record["route"]["runtime"] == owner["runtime"]
        if record.get("created"):
            atomic_write_json(directory / "request.json", request)
            if native:
                atomic_write_json(directory / "native.json", {"mode": "native", "owner": owner})
            atomic_write_json(submission, {"key": record["key"], "intent": request["submissionIntent"]}, create_parents=True)
            if not native:
                try:
                    (launch or launch_supervisor)(store, record, directory)
                except BaseException as error:
                    store.finish(record["key"], record["token"], {"ok": False, "error": str(error)}, status="needs-inspection")
                    raise
    return {**record, "native": native, "actualRole": request["actualRole"], "brief": request["prompt"] if native else None}


def launch_supervisor(store, record, directory):
    argv = [sys.executable, "-P", str(PLUGIN_ROOT / "worker.py"), "--home", str(store.root),
            "supervise", "--key", record["key"], "--token", record["token"]]
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    env = python_environment(child_env(record["request"]["project"]))
    with (directory / "supervisor.log").open("ab") as log:
        process = subprocess.Popen(argv, cwd=record["request"]["project"], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **flags)
    atomic_write_json(directory / "launch.json", {"pid": process.pid, "processStart": process_start(process.pid)})


def require_record(store, key, token=None):
    record = store.get(key, reconcile=False)
    if not record or token is not None and record.get("token") != token:
        raise ValueError("worker job or attempt token does not match")
    return record


def supervise(key, token, *, store=None):
    store = store or Store()
    record = require_record(store, key, token)
    directory = attempt_directory(store, record)
    request = read_json(directory / "request.json")
    if request != record["request"]:
        raise ValueError("worker request file differs from reservation")
    if (directory / "native.json").exists():
        raise ValueError("native jobs must run through the host's native agent tool")
    store.start(key, token, os.getpid())
    try:
        adapter, owner = ownership(request)
        if digest(adapter) != request["adapterDigest"] or implementation_digest() != request["implementation"]:
            raise ValueError("worker configuration or implementation changed before launch")
        if snapshot(request["project"], request["inputPaths"]) != request["before"]:
            raise ValueError("worker inputs changed before launch")
        if snapshot(request["project"], request["files"]) != request["beforeFiles"]:
            raise ValueError("worker output files changed before launch")
        owner.update(files=request["files"], inputPaths=request["inputPaths"])
        owner["cancelPath"] = str(directory / "cancel.json")
        if request.get("resumeSession"):
            previous = require_record(store, request["previous"])
            if previous["route"]["runtime"] == record["route"]["runtime"]:
                owner["resumeSession"] = request["resumeSession"]
        deadline = time.monotonic() + request["timeout"]

        def cancelled():
            if (directory / "cancel.json").exists() or time.monotonic() >= deadline:
                return True
            try:
                ownership(request)
            except (OSError, ValueError):
                return True
            return False

        def started(pid):
            atomic_write_json(directory / "child.json", {"pid": pid, "processStart": process_start(pid)})

        tried = set()
        while True:
            runtime = record["route"]["runtime"]
            tried.add(runtime)
            owner["expectedPolicy"] = record["route"].get("policy", "")
            run_directory = directory / "runs" / runtime
            run_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                result = backends.run(runtime, request["role"], request["prompt"],
                                      cwd=request["project"], owner=owner,
                                      settings={**adapter.get("workers", {}), "projectRoles": effective_roles(adapter, request)},
                                      timeout=max(1, int(deadline - time.monotonic())), context_dir=run_directory,
                                      cancelled=cancelled, on_start=started)
                if result.get("errorCode") == "rate_limited":
                    failure = {"runtime": runtime, "errorCode": "rate_limited", "beforeWork": False,
                               "error": result.get("error", "worker reached the service rate limit")}
                    if result.get("retryAfter") is not None:
                        failure["retryAfter"] = result["retryAfter"]
                    store.note_rate_limit(runtime, failure, profile=record["route"].get("profile"))
                elif result.get("ok") is True:
                    store.note_model_success(runtime, profile=record["route"].get("profile"))
                break
            except backends.RateLimited as error:
                if (not error.before_work or error.runtime != runtime or cancelled()
                        or snapshot(request["project"], request["files"]) != request["beforeFiles"]
                        or snapshot(request["project"], request["inputPaths"]) != request["before"]):
                    raise ValueError("rate-limited worker cannot safely reroute after changes or cancellation") from error
                current_adapter, _ = ownership(request)
                if digest(current_adapter) != request["adapterDigest"]:
                    raise ValueError("worker adapter changed before rate-limit rerouting") from error
                failure = {**error.evidence, "beforeWork": True}
                store.note_rate_limit(runtime, failure, profile=record["route"].get("profile"))
                candidates = available_candidates(adapter, request, owner)
                try:
                    record = store.reroute(key, token, candidates, failure, exclude=tried)
                except ValueError as unavailable:
                    return store.finish(key, token, {"ok": False, "errorCode": "rate_limited",
                                                    "error": str(unavailable), "lastFailure": error.evidence})
                owner.pop("resumeSession", None)
                if record["route"]["runtime"] == owner["runtime"]:
                    atomic_write_json(directory / "native.json", {"mode": "native", "owner": owner,
                                                                  "handoff": "rate-limit fallback"})
                    return {**record, "native": True, "actualRole": request["actualRole"], "brief": request["prompt"]}
        ownership(request)
        after = snapshot(request["project"], request["inputPaths"])
        result["before"] = request["before"]
        result["after"] = after
        result["files"] = snapshot(request["project"], request["files"])
        if request["role"].removeprefix("chef:") in READ_ONLY and after != request["before"]:
            result.update(ok=False, error="read-only worker inputs changed during execution")
        state = "cancelled" if (directory / "cancel.json").exists() else None
        if result.get("ok") is not True and result["files"] != request["beforeFiles"]:
            state = "needs-inspection"
        return store.finish(key, token, result, status=state)
    except BaseException as error:
        store.finish(key, token, {"ok": False, "error": str(error)}, status="needs-inspection")
        raise


def status(key, *, store=None):
    store = store or Store()
    record = require_record(store, key)
    directory = attempt_directory(store, record)
    if (directory / "native.json").exists():
        return {**record, "native": True, "actualRole": record["request"]["actualRole"],
                "brief": record["request"]["prompt"]}
    launch = directory / "launch.json"
    if record["status"] == "reserved" and launch.exists():
        if not process_owner_alive(read_json(launch)):
            return store.finish(key, record["token"], {"ok": False, "error": "supervisor ended before claiming work"}, status="needs-inspection")
    elif record["status"] == "reserved" and not process_owner_alive(record):
        return store.finish(key, record["token"], {"ok": False, "error": "dispatch ended before recording supervisor ownership"}, status="needs-inspection")
    return store.get(key)


def cancel(key, token, *, store=None):
    store = store or Store()
    record = require_record(store, key, token)
    ownership(record["request"])
    directory = attempt_directory(store, record)
    if record["status"] not in ("reserved", "running"):
        return record
    atomic_write_json(directory / "cancel.json", {"cancel": True})
    if (directory / "native.json").exists():
        return {**record, "cancelNative": True,
                "instruction": "Cancel and collect the native agent before submitting its cancellation receipt."}
    return {**record, "cancellationRequested": True}


def native_result(key, token, result, *, store=None):
    store = store or Store()
    record = require_record(store, key, token)
    request = record["request"]
    adapter, _ = ownership(request)
    directory = attempt_directory(store, record)
    if not (directory / "native.json").exists():
        raise ValueError("job is not a native host dispatch")
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("native result requires an explicit boolean ok")
    evidence = project_paths(request["project"], result.get("evidence", []), "evidence")
    if result["ok"] and not evidence:
        raise ValueError("successful native result requires evidence files")
    pointers = snapshot(request["project"], evidence)
    if any(value is None for value in pointers.values()):
        raise ValueError("native evidence files must exist")
    after = snapshot(request["project"], request["inputPaths"])
    output = {**result, "evidence": pointers, "before": request["before"], "after": after,
              "files": snapshot(request["project"], request["files"])}
    atomic_write_json(directory / "native-collected.json", {"evidence": pointers, "files": output["files"]})
    reason = ""
    if record["route"].get("policy"):
        config = backends.role_config(record["route"]["runtime"], request["role"],
                                      {**adapter.get("workers", {}), "projectRoles": effective_roles(adapter, request)})
        if backends.role_policy(config) != record["route"]["policy"]:
            reason = "native worker role policy changed before result acceptance"
    if request["role"].removeprefix("chef:") in READ_ONLY and after != request["before"]:
        reason = "native review inputs changed; its result cannot be accepted"
    if reason:
        store.finish(key, token, {**output, "ok": False, "error": reason}, status="needs-inspection")
        raise ValueError(reason)
    state = "cancelled" if (directory / "cancel.json").exists() else None
    if result["ok"] is not True and output["files"] != request["beforeFiles"]:
        state = "needs-inspection"
    return store.finish(key, token, output, status=state)


def recover_owner(key, token, runtime, session, *, store=None):
    store = store or Store()
    record = require_record(store, key, token)
    updated = {**record["request"], "ownerRuntime": runtime, "ownerSession": session}
    ownership(updated)
    return store.recover_owner(key, token, owner_runtime=runtime, owner_session=session)


def wait(key, *, seconds=30, store=None):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("wait seconds must be 1..3600")
    store = store or Store()
    deadline = time.monotonic() + seconds
    while True:
        record = status(key, store=store)
        if record.get("native") or record["status"] not in ("reserved", "running") or time.monotonic() >= deadline:
            return record
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))


def reconcile(key, token, evidence, *, store=None):
    """Release inspected partial work only after its tracked execution is dead."""
    store = store or Store()
    record = require_record(store, key, token)
    request = record["request"]
    ownership(request)
    if record["status"] in ("completed", "failed", "cancelled"):
        return record
    directory = attempt_directory(store, record)
    native = (directory / "native.json").exists()
    if native and not (directory / "native-collected.json").exists():
        raise ValueError("collect the native agent result before reconciling its file scope")
    if not native:
        if record["status"] == "running" and process_owner_alive(record):
            raise ValueError("worker supervisor is still alive")
        for name in ("launch.json", "child.json"):
            path = directory / name
            if path.exists() and process_owner_alive(read_json(path)):
                raise ValueError("tracked worker process is still alive")
        if record["status"] == "reserved" and not (directory / "launch.json").exists() and process_owner_alive(record):
            raise ValueError("worker dispatcher may still be launching")
    names = project_paths(request["project"], evidence, "inspection evidence")
    pointers = snapshot(request["project"], names)
    if not pointers or any(value is None for value in pointers.values()):
        raise ValueError("inspection requires existing evidence files")
    result = {"ok": False, "error": "partial work inspected and reservation released",
              "inspection": pointers, "files": snapshot(request["project"], request["files"]),
              "previousResult": record.get("result")}
    return store.finish(key, token, result, status="failed")
