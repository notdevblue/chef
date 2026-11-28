"""Owned, idempotent cross-runtime work with durable detached execution."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from chef.guards import mission
from chef.infrastructure import configuration, paths
from chef.infrastructure.fingerprints import execution_digest as implementation_digest
from chef.infrastructure.errors import UsageError
from chef.infrastructure.locking import FileLock, LockBusy
from chef.infrastructure.process import child_env
from chef.infrastructure.python import python_environment
from chef.infrastructure.storage import atomic_write_json, make_private_directory
from chef.locations import PLUGIN_ROOT
from chef.mission import unit_contracts
from chef.mission.contracts import StateError, UNIT_HEADER, UNITS_FILE
from chef.mission.storage import read_table, require_unit
from chef.verification.contracts import VerificationError
from chef.workers import backends, policy, review_results
from chef.workers.routing import role_name
from chef.workers.store import Store, request_identity
from chef.verification.execution import process_owner_alive, process_start

RUNTIMES = ("claude", "codex", "grok")
MODES = policy.MODES
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
            if paths.is_link(current) and (label != "inputPaths" or not paths.inside_real_root(project, current)):
                raise ValueError(f"{label} cannot traverse links")
        if absolute.exists() and not absolute.is_file() and not (label == "inputPaths" and absolute.is_dir()):
            raise ValueError(f"{label} accepts exact files only")
        result.append(relative.as_posix())
    return list(dict.fromkeys(result))


def snapshot(project, values):
    result = {}
    for name in project_paths(project, values, "snapshot"):
        path = Path(project, name)
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def input_snapshot(project, values):
    result = {}
    for name in project_paths(project, values, "inputPaths"):
        path = Path(project, name)
        if not path.is_dir():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            continue
        entries = {}
        pending, seen = [(path, Path(), frozenset())], 0
        while pending:
            child, relative, ancestors = pending.pop()
            seen += 1
            if seen > 250000:
                raise ValueError("input directory exceeds 250000 entries")
            if not paths.inside_real_root(project, child):
                raise ValueError("input directory escaped the project through a link")
            if child.is_dir():
                info = child.stat()
                identity = (info.st_dev, info.st_ino) if info.st_ino else os.path.realpath(child)
                if identity in ancestors:
                    raise ValueError("input directory contains a link cycle")
                pending.extend((item, relative / item.name, ancestors | {identity}) for item in child.iterdir())
            elif child.is_file():
                entries[relative.as_posix()] = hashlib.sha256(child.read_bytes()).hexdigest()
            if len(entries) > 10000:
                raise ValueError("input directory exceeds 10000 files")
        result[name] = digest(entries)
    return result


def observed_snapshot(project, values):
    """Record changed path shapes without following or authorizing them.

    Admission remains file-only. A previously missing output can become a
    directory or link, including in requests saved by older installations.
    Such a path must retain its reservation until inspection, but must not
    prevent collecting the stopped worker's result.
    """
    result = {}
    for name in string_list(values, "snapshot"):
        try:
            result.update(snapshot(project, [name]))
        except ValueError as error:
            result[name] = {"kind": "invalid-path", "error": str(error)}
        except OSError as error:
            result[name] = {"kind": "unreadable", "error": type(error).__name__}
    return result


def collect_snapshots(request, result):
    after = {}
    for name in request["inputPaths"]:
        try:
            after.update(input_snapshot(request["project"], [name]))
        except (ValueError, OSError) as error:
            after[name] = {"kind": "invalid-path", "error": str(error)}
    output = {**result, "before": request["before"],
              "after": after,
              "files": observed_snapshot(request["project"], request["files"])}
    for name in request["inputPaths"]:
        path = Path(request["project"], name)
        if path.is_dir() and request.get("inputKinds", {}).get(name) != "directory":
            output["after"][name] = {"kind": "invalid-path", "error": "input became a directory after admission"}
    if any(isinstance(value, dict) for field in ("after", "files") for value in output[field].values()):
        for name in ("error", "errorCode"):
            if name in result:
                output["worker" + name[0].upper() + name[1:]] = result[name]
        output.update(ok=False, errorCode="path_snapshot_failed",
                      error="worker paths could not be snapshotted; inspect the recorded path errors")
    return output


def ownership(request, *, brief=False):
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
    if (not active.corrupt and active.id == request.get("mission")
            and active.session == request.get("ownerSession") and not active.runtime):
        raise ValueError("ACTIVE runtime is missing; run mission_control.py resume before dispatch")
    if (active.corrupt or not mission.valid_mission_id(active.id) or active.id != request.get("mission")
            or active.session != request.get("ownerSession")
            or active.runtime != runtime):
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
    owner = {"runtime": runtime, "session": active.session, "mission": active.id,
             "state": state, "unitContract": binding,
             "timeboxSeconds": int(row["timebox"]) * 60 if str(row.get("timebox", "")).isdecimal() else 0}
    if brief:
        owner["brief"] = {field: row[field] for field in ("goal", "files", "done", "verify", "forbid", "human")}
    return adapter, owner


def prepare(value):
    if not isinstance(value, dict):
        raise ValueError("worker request must be an object")
    request = dict(value)
    for flag in ("managedExecution", "recordReview", "closeLow"):
        if type(request.get(flag, False)) is not bool:
            raise ValueError(f"{flag} must be a boolean")
    if request.get("recordReview") and not (request.get("managedExecution") and request.get("reviewKind")):
        raise ValueError("automatic review recording requires a supervised bound review")
    if request.get("closeLow") and not request.get("recordReview"):
        raise ValueError("closeLow requires automatic review recording")
    for key in ("requestId", "unit", "role", "ownerSession", "mission"):
        text(request.get(key), key)
    reuse = request.pop("useStoredBrief", False)
    if type(reuse) is not bool:
        raise ValueError("useStoredBrief must be a boolean")
    prompt = request.get("prompt", "" if reuse else None)
    if not isinstance(prompt, str) or (not reuse and not prompt.strip()) or "\x00" in prompt:
        raise ValueError("prompt must be nonempty text unless using the stored brief")
    timeout = request.get("timeout", 600)
    if type(timeout) is not int or not 10 <= timeout <= 86400:
        raise ValueError("timeout must be 10..86400 seconds")
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
    adapter, owner = ownership(request, brief=True) if reuse else ownership(request)
    if reuse:
        brief = owner.pop("brief")
        content = "\n".join(f"{field}: {value}" for field, value in brief.items())
        request["prompt"] = f"Approved unit {request['unit']}:\n{content}"
    else:
        request["prompt"] = ""
    extra = review_results.prepare(request, owner, adapter.get("verify") or {}, prompt)
    if extra.strip():
        request["prompt"] = (request["prompt"] + "\n\nRole-specific instructions:\n" if reuse else "") + extra
    settings = adapter.get("workers", {})
    if not isinstance(settings, dict):
        raise ValueError("workers must be an object")
    maximum = settings.get("maxTimeoutSeconds", 7200)
    if "timeout" not in value and type(maximum) is int:
        timeout = min(timeout, maximum, owner.get("timeboxSeconds") or maximum)
        request["timeout"] = timeout
    if type(maximum) is not int or not 10 <= maximum <= 86400 or timeout > maximum:
        raise ValueError("worker timeout exceeds workers.maxTimeoutSeconds")
    if owner.get("timeboxSeconds", 0) > 0 and timeout > owner["timeboxSeconds"]:
        raise ValueError("worker timeout exceeds the approved unit timebox")
    resolved = policy.resolve(request, settings, owner_runtime=owner["runtime"], role=request["role"])
    request["mode"] = resolved["mode"]
    request["allowedRuntimes"] = resolved["allowedRuntimes"]
    request["routingPolicy"] = {name: resolved[name] for name in ("runtimes", "roleRouting")}
    request["unitContract"] = owner["unitContract"]
    role_key = requested_role if requested_role in ("audit", "research", "implement", "implementSmall", "verify", "review", "reviewSmall") else ROLE_KEYS[request["role"]]
    roles = adapter.get("roles", {})
    default_role = roles.get(role_key, roles.get("review") if role_key == "audit" else None) or "chef:" + request["role"]
    actual_role = text(request.get("actualRole", default_role), "actual role")
    if actual_role not in (default_role, "chef:" + request["role"]):
        raise ValueError("actualRole must preserve the requested role capability")
    from chef.guards.subagents import allow_patterns, allows

    patterns, problem = allow_patterns(adapter)
    if problem or (patterns is not None and not allows(patterns, actual_role)):
        raise ValueError(problem or "actualRole is outside agents.allow")
    request["actualRole"] = actual_role
    from chef.workers.verification import bind

    request["verification"] = bind(request, owner)
    project = request["project"]
    request["files"] = project_paths(project, request.get("files", []), "files")
    request["inputPaths"] = project_paths(project, request.get("inputPaths", []), "inputPaths")
    request["inputKinds"] = {name: "directory" if Path(project, name).is_dir() else "file" for name in request["inputPaths"]}
    role = request["role"].removeprefix("chef:")
    if role not in READ_ONLY and role != "verifier" and not request["files"]:
        raise ValueError("write workers require an explicit files scope")
    for key in ("requiredTools", "hostTools", "hostRoles"):
        request[key] = string_list(request.get(key, []), key)
    if (request.get("managedExecution") and request["verification"]
            and not {"Bash", "PowerShell"}.intersection(request["requiredTools"])):
        request["requiredTools"].append("Bash")
    for name in request["files"]:
        target = Path(project, name)
        if paths.same_path(target, owner["state"]) or paths.inside_real_root(owner["state"], target):
            raise ValueError("worker files cannot include mission control state")
        if any(part.casefold() in (".git", ".claude", ".codex", ".grok", ".chef") for part in Path(name).parts):
            raise ValueError("worker files cannot include executable policy or repository metadata")
    request["before"] = input_snapshot(project, request["inputPaths"])
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
    native_runtime = None if request.get("managedExecution") else owner["runtime"]
    candidates = backends.candidates({**adapter, "roles": effective_roles(adapter, request)},
                                    request["project"], request["role"],
                                    native=native_runtime, host_tools=request["hostTools"],
                                    allowed_runtimes=allowed)
    candidates = [candidate for candidate in candidates if candidate["runtime"] in allowed]
    for candidate in candidates:
        candidate.setdefault("roles", [request["role"]])
        candidate.setdefault("tools", candidate.get("capabilities", []))
        if candidate["runtime"] == native_runtime:
            if request["actualRole"] not in request["hostRoles"]:
                candidate.update(available=False, error="native role is not observed in the host role registry")
            elif not candidate.get("available") and request["actualRole"] not in {"chef:" + name for name in ROLE_KEYS}:
                candidate.update(available=True, tools=request["hostTools"],
                                 model="unknown", effort="unknown", policy="")
    return candidates


def attempt_directory(store, record):
    directory = Path(store.root) / "attempts" / record["key"] / record["token"]
    make_private_directory(directory)
    return directory


def overlaps(project, first, second):
    def identity(name):
        path = Path(project, name)
        try:
            value = path.stat()
            return ("file", value.st_dev, value.st_ino)
        except (FileNotFoundError, NotADirectoryError):
            return ("path", paths.canonical_path(path))
    if {identity(name) for name in first} & {identity(name) for name in second}:
        return True
    # A missing exact-file scope may later become a directory. Keep its
    # descendants reserved too, until the original work has been inspected.
    first_paths = [paths.canonical_path(Path(project, name)) for name in first]
    second_paths = [paths.canonical_path(Path(project, name)) for name in second]
    return any(left == right or paths.inside_root(left, right) or paths.inside_root(right, left)
               for left in first_paths for right in second_paths)


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


def existing_submission(store, request, submission, *, source_intent=None):
    if not submission.exists():
        return None
    saved = read_json(submission)
    identity, intent = ("sourceIntent", source_intent) if source_intent else ("intent", request["submissionIntent"])
    if saved.get(identity) != intent:
        raise ValueError("requestId was already submitted with different intent; use a new requestId")
    record = require_record(store, saved["key"])
    if any(record["request"].get(name) != request[name] for name in ("ownerRuntime", "ownerSession")):
        raise ValueError("recover the worker owner before retrieving this submission")
    native = (attempt_directory(store, record) / "native.json").exists()
    return {**record, "created": False, "native": native, "actualRole": record["request"]["actualRole"],
            "brief": record["request"]["prompt"] if native else None}


def submission_paths(store, request):
    workspace = os.stat(request["project"])
    identity = digest({"workspace": [workspace.st_dev, workspace.st_ino],
                       "mission": request["mission"], "requestId": request["requestId"]})
    return (Path(store.root) / "submissions" / (identity + ".json"),
            str(Path(store.root) / "admission" / (digest([workspace.st_dev, workspace.st_ino]) + ".lock")))


def dispatch(value, *, store=None, launch=None):
    store = store or Store()
    source_intent = None
    if isinstance(value, dict) and value.get("managedExecution") is True:
        text(value.get("requestId"), "requestId")
        text(value.get("unit"), "unit")
        ownership(value)
        source_intent = digest(value)
        submission, gate = submission_paths(store, value)
        with FileLock(gate, "worker admission", timeout=10):
            existing = existing_submission(store, value, submission, source_intent=source_intent)
            if existing is not None:
                return existing
    request, adapter, owner = prepare(value)
    submission, gate = submission_paths(store, request)
    with FileLock(gate, "worker admission", timeout=10):
        existing = existing_submission(store, request, submission, source_intent=source_intent)
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
        prior_is_native = (attempt_directory(store, prior) / "native.json").is_file()
        now_native = not request.get("managedExecution") and runtime == owner["runtime"]
        if (candidate and prior_is_native == now_native
                and prior["route"].get("policy", "") == candidate.get("policy", "") and session):
            request["resumeSession"] = text(session, "previous session")
            previous = {"runtime": runtime}
    with FileLock(gate, "worker admission", timeout=10):
        existing = existing_submission(store, request, submission, source_intent=source_intent)
        if existing is not None:
            return existing
        current_adapter, _ = ownership(request)
        if digest(current_adapter) != request["adapterDigest"]:
            raise ValueError("worker adapter changed during admission")
        if (input_snapshot(request["project"], request["inputPaths"]) != request["before"]
                or snapshot(request["project"], request["files"]) != request["beforeFiles"]):
            raise ValueError("worker files changed during admission")
        check_conflicts(store, request)
        record = store.reserve(request, candidates, previous=previous,
                               independent=bool(request.get("independent", False)))
        directory = attempt_directory(store, record)
        native = not request.get("managedExecution") and record["route"]["runtime"] == owner["runtime"]
        if record.get("created"):
            atomic_write_json(directory / "request.json", request)
            if native:
                atomic_write_json(directory / "native.json", {"mode": "native", "owner": owner})
            saved = {"key": record["key"], "intent": request["submissionIntent"]}
            if source_intent:
                saved["sourceIntent"] = source_intent
            atomic_write_json(submission, saved, create_parents=True)
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


def review_record_state(directory, record):
    if not record["request"].get("recordReview") or record["status"] != "completed":
        return record
    marker = directory / "review-record.json"
    if marker.is_file():
        return {**record, "reviewRecord": read_json(marker)}
    launch = directory / "launch.json"
    if launch.is_file() and process_owner_alive(read_json(launch)):
        review = {"status": "pending"}
    else:
        review = {"status": "interrupted", "error": "supervisor stopped before confirming the review record"}
    return {**record, "reviewRecord": review}


def finalize_review(store, record):
    try:
        path = review_results.record_owned(record["request"], record["key"], store)
        recorded = {"status": "recorded", "path": path}
    except (VerificationError, StateError, UsageError, LockBusy, ValueError, OSError) as error:
        recorded = {"status": "blocked", "error": str(error)}
    atomic_write_json(attempt_directory(store, record) / "review-record.json", recorded, durable=True)
    return {**record, "reviewRecord": recorded}


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
        if input_snapshot(request["project"], request["inputPaths"]) != request["before"]:
            raise ValueError("worker inputs changed before launch")
        if snapshot(request["project"], request["files"]) != request["beforeFiles"]:
            raise ValueError("worker output files changed before launch")
        owner.update(files=request["files"], inputPaths=request["inputPaths"], verification=request.get("verification"))
        if request.get("reviewBinding"):
            owner["structuredReview"] = True
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
            make_private_directory(run_directory)
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
                        or input_snapshot(request["project"], request["inputPaths"]) != request["before"]):
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
                if not request.get("managedExecution") and record["route"]["runtime"] == owner["runtime"]:
                    atomic_write_json(directory / "native.json", {"mode": "native", "owner": owner,
                                                                  "handoff": "rate-limit fallback"})
                    return {**record, "native": True, "actualRole": request["actualRole"], "brief": request["prompt"]}
        ownership(request)
        result = collect_snapshots(request, result)
        if request.get("reviewBinding") and result.get("ok") is True:
            try:
                result["review"] = review_results.payload(result)
            except VerificationError as error:
                result.update(ok=False, errorCode="invalid_review_result", error=str(error))
        if request["role"].removeprefix("chef:") in READ_ONLY and result["after"] != request["before"]:
            result.update(ok=False, error="read-only worker inputs changed during execution")
        state = "cancelled" if (directory / "cancel.json").exists() else None
        if result.get("ok") is not True and (result["files"] != request["beforeFiles"]
                                              or result.get("errorCode") == "path_snapshot_failed"):
            state = "needs-inspection"
        finished = store.finish(key, token, result, status=state)
    except BaseException as error:
        store.finish(key, token, {"ok": False, "error": str(error)}, status="needs-inspection")
        raise
    if finished["status"] == "completed" and request.get("recordReview"):
        return finalize_review(store, finished)
    return finished


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
    return review_record_state(directory, store.get(key))


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
    if request.get("reviewBinding") and isinstance(result, dict) and "ok" not in result and "schema" in result:
        result = {"ok": True, "review": result}
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("native result requires an explicit boolean ok")
    if request.get("reviewBinding") and result["ok"]:
        result = {**result, "review": review_results.payload(result)}
    evidence = project_paths(request["project"], result.get("evidence", []), "evidence")
    if result["ok"] and not evidence and not request.get("reviewBinding"):
        raise ValueError("successful native result requires evidence files")
    pointers = snapshot(request["project"], evidence)
    if any(value is None for value in pointers.values()):
        raise ValueError("native evidence files must exist")
    output = collect_snapshots(request, {**result, "evidence": pointers})
    atomic_write_json(directory / "native-collected.json", {"evidence": pointers, "files": output["files"],
                                                            "after": output["after"]})
    reason = ""
    if record["route"].get("policy"):
        config = backends.role_config(record["route"]["runtime"], request["role"],
                                      {**adapter.get("workers", {}), "projectRoles": effective_roles(adapter, request)})
        if backends.role_policy(config) != record["route"]["policy"]:
            reason = "native worker role policy changed before result acceptance"
    if request["role"].removeprefix("chef:") in READ_ONLY and output["after"] != request["before"]:
        reason = "native review inputs changed; its result cannot be accepted"
    if reason:
        store.finish(key, token, {**output, "ok": False, "error": reason}, status="needs-inspection")
        raise ValueError(reason)
    state = "cancelled" if (directory / "cancel.json").exists() else None
    if output["ok"] is not True and (output["files"] != request["beforeFiles"]
                                      or output.get("errorCode") == "path_snapshot_failed"):
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
        pending_review = record.get("reviewRecord", {}).get("status") == "pending"
        if (record.get("native") or (record["status"] not in ("reserved", "running") and not pending_review)
                or time.monotonic() >= deadline):
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
              "inspection": pointers, "files": observed_snapshot(request["project"], request["files"]),
              "previousResult": record.get("result")}
    return store.finish(key, token, result, status="failed")
