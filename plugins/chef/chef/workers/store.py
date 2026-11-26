"""Durable, token-fenced worker jobs shared by local Chef sessions.

    Store.reserve(request, candidates) returns {created, key, token, route, ...}.
    start records the supervisor PID; finish publishes the result. Repeating a
    request returns the existing job even if it failed or its owner disappeared.
    Explicit new attempts require a changed request (for example attempt=2), so
    uncertain partial edits are never retried merely because a process died.

Requests require project, mission, unit, role, inputDigest, ownerRuntime and
ownerSession. Additional JSON fields such as prompt, condition, requiredTools,
attempt, previous and independent participate in the idempotency identity.
Quality can only be added explicitly by a coordinator after independent checks.
The registry contains compact metadata and hashes of immutable request/result
payloads. Payloads are published durably before their references, and legacy
inline records are hydrated and migrated on the next mutation.
"""

import copy
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from chef.infrastructure.locking import FileLock
from chef.infrastructure.storage import atomic_write_bytes, atomic_write_json
from chef.verification.execution import process_owner_alive, process_start
from chef.workers import routing

STATUSES = {"reserved", "running", "completed", "failed", "needs-inspection", "cancelled"}
TERMINAL = {"completed", "failed", "cancelled"}
MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
REQUEST_SUMMARY = ("project", "mission", "unit", "role", "inputDigest", "ownerRuntime", "ownerSession",
                   "files", "inputPaths", "condition")


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def request_identity(request):
    if not isinstance(request, dict):
        raise ValueError("worker request must be an object")
    value = copy.deepcopy(request)
    for name in ("project", "mission", "unit", "inputDigest", "ownerSession"):
        if not isinstance(value.get(name), str) or not value[name].strip() or any(
                char in value[name] for char in "\x00\r\n"):
            raise ValueError(f"worker request requires {name}")
    if value.get("ownerRuntime") not in routing.RUNTIMES:
        raise ValueError("unsupported owner runtime")
    root = Path(value["project"])
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("worker project must be an existing absolute directory")
    value["project"] = str(root.resolve())
    value["role"] = routing.role_name(value.get("role"))
    identity = root.stat()
    # Existing filesystem identity keeps aliases of a workspace in one job.
    stable = {name: item for name, item in value.items() if name not in ("ownerRuntime", "ownerSession", "ownerPid")}
    encoded = canonical({**stable, "project": [identity.st_dev, identity.st_ino]})
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("worker request exceeds size limit")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), value


class Store:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("CHEF_WORKERS_HOME") or Path.home() / ".chef/workers").absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "registry.json"

    def _lock(self):
        return FileLock(self.root / "registry.lock", "Chef worker registry", timeout=10)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("worker registry cannot be a symbolic link")
        if not self.path.exists():
            return {"schema": 1, "cursor": 0, "jobs": {}, "quality": [], "qualityJobs": [], "serviceHealth": {}}
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("worker registry exceeds size limit")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema") != 1 or type(value.get("cursor")) is not int
                or value["cursor"] < 0 or not isinstance(value.get("jobs"), dict)
                or not isinstance(value.get("quality"), list) or not isinstance(value.get("qualityJobs"), list)):
            raise ValueError("invalid worker registry")
        health = value.setdefault("serviceHealth", {})
        if not isinstance(health, dict):
            raise ValueError("invalid worker service health")
        for runtime, row in health.items():
            if (not isinstance(runtime, str) or not re.fullmatch(r"(?:claude|codex|grok)(?::[0-9a-f]{64})?", runtime)
                    or not isinstance(row, dict)
                    or row.get("reason") != "rate_limited" or type(row.get("retryAfter")) not in (int, float)
                    or type(row.get("failures")) is not int or row["failures"] < 1):
                raise ValueError("invalid worker service health record")
        for key, job in value["jobs"].items():
            if (not isinstance(job, dict) or job.get("key") != key or job.get("status") not in STATUSES
                    or not isinstance(job.get("token"), str) or not isinstance(job.get("request"), dict)
                    or (job.get("route") or {}).get("runtime") not in routing.RUNTIMES):
                raise ValueError("invalid worker job record")
            if "requestRef" in job:
                self._payload_path(job["requestRef"])
                if job.get("requestMetadataVersion") != 2:
                    # Older indices omitted conflict scope. Load only their
                    # requests for migration, never their historical results.
                    request = self._load_request(job)
                    job["request"] = {name: request.get(name) for name in REQUEST_SUMMARY}
                    job["requestMetadataVersion"] = 2
            if "resultRef" in job:
                self._payload_path(job["resultRef"])
        return value

    def _load_request(self, job):
        request = self._read_payload(job["requestRef"], MAX_REQUEST_BYTES)
        if not isinstance(request, dict) or any(request.get(name) != expected
                for name, expected in job["request"].items() if name in REQUEST_SUMMARY):
            raise ValueError("worker request payload differs from its index")
        return request

    def _hydrate(self, job):
        if "requestRef" in job:
            job["request"] = self._load_request(job)
            job.pop("requestRef")
        if "resultRef" in job:
            job["result"] = self._read_payload(job.pop("resultRef"), MAX_RESULT_BYTES)
            if not isinstance(job["result"], dict):
                raise ValueError("worker result payload must be an object")
        return job

    def _write(self, value):
        # Payloads are immutable and durable before the index publishes their
        # references. A crash may leave an unreferenced payload, never a record
        # pointing to an incomplete write.
        index = copy.deepcopy(value)
        for job in index["jobs"].values():
            if "requestRef" not in job:
                request = job["request"]
                job["requestRef"] = self._write_payload(request, MAX_REQUEST_BYTES)
                job["request"] = {name: request.get(name) for name in REQUEST_SUMMARY}
                job["requestMetadataVersion"] = 2
            if "result" in job:
                job["resultRef"] = self._write_payload(job.pop("result"), MAX_RESULT_BYTES)
        if len((json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("worker registry exceeds size limit")
        atomic_write_json(self.path, index, durable=True)

    def _payload_path(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f]{64}", reference):
            raise ValueError("invalid worker payload reference")
        directory = self.root / "payloads"
        if directory.is_symlink():
            raise ValueError("worker payload directory cannot be a symbolic link")
        return directory / (reference + ".json")

    def _read_payload(self, reference, limit):
        path = self._payload_path(reference)
        if path.is_symlink():
            raise ValueError("worker payload cannot be a symbolic link")
        try:
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
        except OSError as error:
            raise ValueError("worker payload is unavailable") from error
        if len(raw) > limit or hashlib.sha256(raw).hexdigest() != reference:
            raise ValueError("worker payload hash or size differs")
        return json.loads(raw)

    def _write_payload(self, value, limit):
        raw = canonical(value).encode("utf-8")
        if len(raw) > limit:
            raise ValueError("worker payload exceeds size limit")
        reference = hashlib.sha256(raw).hexdigest()
        path = self._payload_path(reference)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            self._read_payload(reference, limit)
        else:
            atomic_write_bytes(path, raw, durable=True)
        return reference

    def _reconcile(self, state):
        changed = False
        for job in state["jobs"].values():
            # Reservation callers are short-lived CLI commands, not worker
            # owners. Only a claimed running supervisor has a process lifetime.
            if job["status"] == "running" and not process_owner_alive(job):
                job.update(status="needs-inspection", updatedAt=now(),
                           recoveryReason="worker owner exited; inspect partial changes before any new attempt")
                changed = True
        return changed

    def reserve(self, request, candidates, *, previous=None, independent=False):
        key, request = request_identity(request)
        with self._lock():
            state = self._read()
            changed = self._reconcile(state)
            if key in state["jobs"]:
                existing = state["jobs"][key]["request"]
                if any(existing[name] != request[name] for name in ("ownerRuntime", "ownerSession")):
                    raise ValueError("worker ownership differs; explicitly recover after validating mission ownership")
                if changed:
                    self._write(state)
                return {**self._hydrate(copy.deepcopy(state["jobs"][key])), "created": False}
            available = self._healthy_candidates(state, candidates)
            route = routing.select(available, role=request["role"],
                required_tools=request.get("requiredTools", ()), jobs=list(state["jobs"].values()),
                quality=state["quality"], previous=previous or request.get("previous"),
                independent=independent or request.get("independent", False), cursor=state["cursor"],
                condition=request.get("condition", ""))
            selected = next(item for item in candidates if item["runtime"] == route["runtime"] and item.get("available") is True)
            route["policy"] = selected.get("policy", "")
            route["model"] = selected.get("model")
            route["effort"] = selected.get("effort")
            route["profile"] = selected.get("profile")
            job = {"key": key, "token": secrets.token_hex(16), "request": request, "route": route,
                   "status": "reserved", "pid": os.getpid(), "processStart": process_start(os.getpid()),
                   "createdAt": now(), "updatedAt": now(), "cancelRequested": False}
            state["jobs"][key] = job
            state["cursor"] = (routing.RUNTIMES.index(route["runtime"]) + 1) % len(routing.RUNTIMES)
            self._write(state)
            return {**copy.deepcopy(job), "created": True}

    def _healthy_candidates(self, state, candidates, *, exclude=()):
        clock = time.time()
        result = []
        for candidate in candidates:
            runtime = candidate.get("runtime")
            health = state["serviceHealth"].get(self._health_key(runtime, candidate.get("profile")), {})
            available = (candidate.get("available") is True and runtime not in exclude
                         and health.get("retryAfter", 0) <= clock)
            result.append({**candidate, "available": available})
        return result

    def _health_key(self, runtime, profile=None):
        if runtime not in routing.RUNTIMES:
            raise ValueError("unsupported worker runtime")
        if profile is None:
            return runtime
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError("worker profile must be a nonempty opaque identifier")
        return runtime + ":" + hashlib.sha256(profile.encode("utf-8")).hexdigest()

    def health(self):
        with self._lock():
            return copy.deepcopy(self._read()["serviceHealth"])

    def note_rate_limit(self, runtime, evidence, *, profile=None):
        """Back off local new attempts after an actual model rate-limit error.

        retryAfter is our bounded retry delay, not an estimate of the account's
        remaining quota or the provider's reset time. Authentication success
        deliberately does not clear it.
        """
        if runtime not in routing.RUNTIMES or not isinstance(evidence, dict) or not evidence:
            raise ValueError("rate limit requires a runtime and observed error evidence")
        canonical(evidence)
        key = self._health_key(runtime, profile)
        with self._lock():
            state = self._read()
            previous = state["serviceHealth"].get(key, {})
            failures = min(previous.get("failures", 0) + 1, 5)
            delay = min(60 * 2 ** (failures - 1), 900)
            row = {"reason": "rate_limited", "retryAfter": time.time() + delay,
                   "observedAt": now(), "failures": failures, "evidence": copy.deepcopy(evidence)}
            state["serviceHealth"][key] = row
            self._write(state)
            return copy.deepcopy(row)

    def note_model_success(self, runtime, *, profile=None):
        """Clear backoff only after an actual successful model invocation."""
        key = self._health_key(runtime, profile)
        with self._lock():
            state = self._read()
            if state["serviceHealth"].pop(key, None) is not None:
                self._write(state)

    def reroute(self, key, token, candidates, evidence, *, exclude=()):
        """Move a proven pre-work failure to an untried runtime, at most once each.

        A same-host selection returns to reserved so the native host can take
        the handoff after its supervisor exits. The logical job/token remain.
        """
        if not isinstance(evidence, dict) or evidence.get("beforeWork") is not True:
            raise ValueError("rerouting requires observed beforeWork evidence")
        canonical(evidence)
        with self._lock():
            state = self._read()
            job = self._owned(state, key, token)
            if job["status"] != "running" or job["cancelRequested"]:
                raise ValueError("only the active noncancelled worker may reroute")
            history = job.get("attemptHistory", [])
            tried = {attempt["route"]["runtime"] for attempt in history} | {job["route"]["runtime"]} | set(exclude)
            job["attemptHistory"] = [*history, {"route": job["route"], "evidence": copy.deepcopy(evidence), "endedAt": now()}]
            job["updatedAt"] = now()
            request = job["request"]
            available = self._healthy_candidates(state, candidates, exclude=tried)
            try:
                route = routing.select(available, role=request["role"],
                    required_tools=request.get("requiredTools", ()), jobs=list(state["jobs"].values()),
                    quality=state["quality"], independent=True, cursor=state["cursor"],
                    condition=request.get("condition", ""))
            except ValueError:
                self._write(state)
                raise
            selected = next(item for item in available if item["runtime"] == route["runtime"])
            route.update({field: selected.get(field) for field in ("policy", "model", "effort", "profile")})
            job.update(route=route, updatedAt=now())
            if route["runtime"] == request["ownerRuntime"]:
                job["status"] = "reserved"
            state["cursor"] = (routing.RUNTIMES.index(route["runtime"]) + 1) % len(routing.RUNTIMES)
            self._write(state)
            return copy.deepcopy(job)

    def _owned(self, state, key, token, *, hydrate=True):
        job = state["jobs"].get(key)
        if not job or not secrets.compare_digest(job["token"], token):
            raise ValueError("worker job ownership differs")
        return self._hydrate(job) if hydrate else job

    def start(self, key, token, pid):
        if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
            raise ValueError("worker PID must be a positive integer")
        with self._lock():
            state = self._read()
            job = self._owned(state, key, token, hydrate=False)
            if job["status"] != "reserved" or job["cancelRequested"]:
                raise ValueError("worker job is not available for launch")
            job.update(status="running", pid=pid, processStart=process_start(pid), updatedAt=now())
            self._write(state)
            result = copy.deepcopy(job)
        return self._hydrate(result)

    def finish(self, key, token, result, status=None):
        if not isinstance(result, dict):
            raise ValueError("worker result must be an object")
        status = status or ("completed" if result.get("ok") is True else "failed")
        if status not in TERMINAL | {"needs-inspection"}:
            raise ValueError("invalid terminal worker status")
        canonical(result)
        with self._lock():
            state = self._read()
            job = self._owned(state, key, token, hydrate=False)
            if job["status"] in TERMINAL:
                self._hydrate(job)
                if job.get("result") == result and job["status"] == status:
                    return copy.deepcopy(job)
                raise ValueError("worker result is already final")
            job.pop("resultRef", None)
            job.update(status=status, result=copy.deepcopy(result), updatedAt=now())
            self._write(state)
            output = copy.deepcopy(job)
        return self._hydrate(output)

    def get(self, key, *, reconcile=True):
        with self._lock():
            state = self._read()
            if reconcile and self._reconcile(state):
                self._write(state)
            job = state["jobs"].get(key)
            if job is None:
                raise ValueError("worker job was not found")
            result = copy.deepcopy(job)
        return self._hydrate(result)

    def list(self, *, metadata=False):
        with self._lock():
            state = self._read()
            if self._reconcile(state):
                self._write(state)
            jobs = copy.deepcopy(list(state["jobs"].values()))
        return jobs if metadata else [self._hydrate(job) for job in jobs]

    def cancel(self, key, token):
        """Request cancellation; the execution supervisor must stop/reap first."""
        with self._lock():
            state = self._read()
            job = self._owned(state, key, token, hydrate=False)
            if job["status"] not in TERMINAL:
                job.update(cancelRequested=True, updatedAt=now())
                self._write(state)
            result = copy.deepcopy(job)
        return self._hydrate(result)

    def recover_owner(self, key, token, *, owner_runtime, owner_session):
        """Rebind a resumed mission only after the caller checks its ACTIVE owner.

        This transfers result access, never starts or retries a worker. The
        previous result and attempt identity remain intact.
        """
        if owner_runtime not in routing.RUNTIMES or not isinstance(owner_session, str) or not owner_session.strip():
            raise ValueError("valid resumed mission owner is required")
        with self._lock():
            state = self._read()
            job = self._owned(state, key, token, hydrate=False)
            if "requestRef" in job:
                job["request"] = self._load_request(job)
                job.pop("requestRef")
            job["request"].update(ownerRuntime=owner_runtime, ownerSession=owner_session)
            job["updatedAt"] = now()
            self._write(state)
            result = copy.deepcopy(job)
        return self._hydrate(result)

    def record_quality(self, key, token, *, success, rework):
        """Record one independently checked outcome, once per terminal job."""
        if type(success) is not bool or type(rework) is not int or rework < 0:
            raise ValueError("quality requires boolean success and nonnegative integer rework")
        with self._lock():
            state = self._read()
            job = self._hydrate(copy.deepcopy(self._owned(state, key, token, hydrate=False)))
            if job["status"] not in TERMINAL:
                raise ValueError("quality requires a terminal worker job")
            for field in ("model", "effort"):
                preference = job["route"].get(field)
                observed = job.get("result", {}).get("observed" + field.title())
                if not routing.known_preference(preference) or observed != preference:
                    raise ValueError("quality requires observed model and effort matching the fixed route")
            if key in state["qualityJobs"]:
                raise ValueError("quality was already recorded for this job")
            identity = {"runtime": job["route"]["runtime"], "role": job["request"]["role"],
                        "condition": job["request"].get("condition", ""), "policy": job["route"].get("policy", ""),
                        "model": job["route"]["model"], "effort": job["route"]["effort"]}
            row = next((row for row in state["quality"] if all(row[name] == value
                        for name, value in identity.items())), None)
            if row is None:
                row = {**identity, "samples": 0, "successes": 0, "rework": 0}
                state["quality"].append(row)
            row["samples"] += 1
            row["successes"] += int(success)
            row["rework"] += rework
            state["qualityJobs"].append(key)
            self._write(state)
            return copy.deepcopy(row)
