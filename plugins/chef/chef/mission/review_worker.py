"""PID-owned review supervisor and bounded live output spooling.

Launch records protect cooperative callers, including the spawn/registration
crash window. They do not constrain arbitrary detached descendant processes.
"""

import contextlib
import json
import os
import re
import signal
import subprocess
import threading
from pathlib import Path

from chef.infrastructure import configuration, paths
from chef.infrastructure.fingerprints import implementation_digest
from chef.infrastructure.process import child_env
from chef.mission import unit_contracts
from chef.mission.contracts import Context, EDITABLE_FIELDS, RUNTIMES, UNIT_HEADER, UNIT_ID, UNITS_FILE
from chef.mission.storage import read_table, require_unit
from chef.verification import execution, plan as plans, references
from chef.verification.contracts import VerificationError, digest

MAX_REQUEST_BYTES = 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024


def read_request(value):
    """Reject malformed requests before touching a reservation or spawning work."""
    from chef.mission.review_dispatch import effective_environment

    try:
        with Path(value).open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds size limit")
        request = json.loads(raw)
        if not isinstance(request, dict) or type(request.get("schema")) is not int or request["schema"] != 1:
            raise ValueError("unsupported request schema")
        for name in ("plan", "settings", "adapter", "command", "before", "runtime", "references", "job", "planSource"):
            if not isinstance(request.get(name), dict):
                raise ValueError(f"invalid {name}")
        for name in ("project", "folder"):
            if not isinstance(request.get(name), str) or not Path(request[name]).is_absolute() or not Path(request[name]).is_dir():
                raise ValueError(f"invalid {name}")
        if not isinstance(request.get("unit"), str) or not UNIT_ID.fullmatch(request["unit"]):
            raise ValueError("invalid unit")
        if request.get("runtimeName") not in RUNTIMES:
            raise ValueError("invalid runtime name")
        job = request["job"]
        if (not isinstance(job.get("key"), str) or not re.fullmatch(r"[0-9a-f]{64}", job["key"])
                or not isinstance(job.get("token"), str) or not re.fullmatch(r"[0-9a-f]{32}", job["token"])):
            raise ValueError("invalid job identity")
        expected = references.resolve_path(request["folder"],
            f"verification/review-jobs/{job['key']}/{job['token']}/request.json")
        if expected != Path(value).resolve():
            raise ValueError("request path differs")
        argv = request.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("invalid argv")
        for item in argv:
            references.text_value(item, "review argv")
        if request["command"] != {"argv": argv, "cwd": "."}:
            raise ValueError("command differs from argv")
        if type(request.get("timeout")) is not int or not 1 <= request["timeout"] <= 3600:
            raise ValueError("invalid timeout")
        if type(request.get("maxLogBytes")) is not int or not 1 <= request["maxLogBytes"] <= MAX_LOG_BYTES:
            raise ValueError("invalid log limit")
        identity = digest({"project": str(Path(request["project"]).resolve()), "plan": request["plan"],
            "settings": request["settings"], "snapshot": request["before"], "command": request["command"],
            "runtime": request["runtime"], "references": request["references"],
            "environment": effective_environment(request["project"], request["plan"]), "timeout": request["timeout"],
            "planSource": request["planSource"], "implementation": request.get("implementation"),
            "maxLogBytes": request["maxLogBytes"], "adapter": request["adapter"], "runtimeName": request["runtimeName"]})
        if request.get("requestIdentity") != identity:
            raise ValueError("request identity differs")
        if request.get("implementation") != implementation_digest():
            raise ValueError("supervisor implementation changed")
        return request
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, RecursionError) as error:
        raise VerificationError(f"invalid review supervisor request: {error}", "plan_invalid") from error


def run_from_file(value):
    request = read_request(value)
    folder, unit, original = request["folder"], request["unit"], request["job"]
    current = execution.read_record(execution.job_path(execution.job_directory(folder), original["key"]))
    if (not current or current.get("token") != original["token"]
            or current.get("purpose") != "review" or current.get("unit") != unit
            or current.get("requestIdentity") != request["requestIdentity"]):
        raise VerificationError("review request is no longer owned", "execution_owner_invalid")
    job = execution.transfer_job(folder, original["key"], original["token"], os.getpid())
    from chef.mission import review_dispatch
    from chef.mission.state import verification_guard

    try:
        if job.get("requestIdentity") != request["requestIdentity"]:
            raise VerificationError("review request identity differs", "execution_owner_invalid")
        located = configuration.locate(request["project"], request["runtimeName"])
        if (not paths.same_path(located.project, request["project"])
                or configuration.read_adapter(located.adapter_path, request["runtimeName"]) != request["adapter"]):
            raise VerificationError("review adapter changed", "review_snapshot_changed")
        guard = verification_guard(Context(request["adapter"], str(Path(folder).parent), Path(folder).name,
                                           request["runtimeName"], request["project"]))
        with execution.unit_execution_lock(folder, unit, owned_job=job):
            row = require_unit(read_table(Path(folder) / UNITS_FILE, UNIT_HEADER), unit)
            if (request["plan"].get("unitBrief") != {name: row[name] for name in EDITABLE_FIELDS}
                    or request["plan"].get("unitContract") != unit_contracts.binding(folder, row)):
                raise VerificationError("review unit contract changed", "review_snapshot_changed")
            pointer = request["planSource"]
            if set(pointer) != {"path", "sha256"} or references.file_pointer(
                    request["project"], pointer["path"], external=True) != pointer:
                raise VerificationError("review plan source changed", "review_snapshot_changed")
            source = references.resolve_path(request["project"], pointer["path"], external=True)
            current_plan = plans.load_plan(request["project"], source, request["settings"].get("grades", []))
            current_plan["unitBrief"] = {name: row[name] for name in EDITABLE_FIELDS}
            contract = unit_contracts.binding(folder, row)
            if contract:
                current_plan["unitContract"] = contract
            if current_plan != request["plan"]:
                raise VerificationError("review plan changed", "review_snapshot_changed")
            return review_dispatch.dispatch(request["project"], folder, unit, request["plan"], guard,
                request["command"], request["argv"], request["timeout"], request["before"], request["runtime"],
                request["references"], request["requestIdentity"], job, request["maxLogBytes"], plan_source=pointer)
    except BaseException:
        current = execution.read_record(execution.job_path(execution.job_directory(folder), job["key"]))
        if (current and current.get("status") == "running" and current.get("token") == job["token"]
                and current.get("pid") == os.getpid()):
            # finish_job refuses a live child or uncertain launch. Never erase
            # those ownership records while reporting the original failure.
            with contextlib.suppress(VerificationError):
                execution.finish_job(folder, job["key"], job["token"], release=True)
        raise


def stop_process(process):
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    process.wait()


def bounded_process(argv, project, stdout, stderr, timeout, limit, *, folder, job, lease):
    """Drain both streams throughout execution, retaining at most limit each."""
    if type(limit) is not int or not 1 <= limit <= MAX_LOG_BYTES:
        raise VerificationError("review log limit is invalid", "plan_invalid")
    counts = [{"seen": 0, "written": 0}, {"seen": 0, "written": 0}]
    errors, drains = [], []
    process, safe_to_clear = None, False

    def drain(pipe, destination, count):
        spool_failed = False
        try:
            while True:
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    break
                count["seen"] += len(chunk)
                retained = chunk[:max(0, limit - count["written"])]
                if retained and not spool_failed:
                    try:
                        destination.write(retained)
                        destination.flush()
                        count["written"] += len(retained)
                    except (OSError, ValueError) as error:
                        spool_failed = True
                        if not errors:
                            errors.append(str(error))
        except OSError as error:
            if not errors:
                errors.append(str(error))
        finally:
            pipe.close()

    with contextlib.ExitStack() as streams:
        destinations = [streams.enter_context(Path(path).open("wb")) for path in (stdout, stderr)]
        execution.begin_job_launch(folder, job["key"], job["token"], lease["token"])
        try:
            try:
                process = subprocess.Popen(argv, cwd=project, env=child_env(str(project)),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=os.name != "nt")
            except OSError:
                safe_to_clear = True  # Popen failed without returning a child.
                raise
            execution.register_job_child(folder, job["key"], job["token"], lease["token"], process.pid)
            drains = [threading.Thread(target=drain, args=(pipe, destination, count), daemon=True)
                for pipe, destination, count in zip((process.stdout, process.stderr), destinations, counts)]
            for thread in drains:
                thread.start()
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process(process)
            for thread in drains:
                thread.join(timeout=2)
            if any(thread.is_alive() for thread in drains):
                stop_process(process)
                for thread in drains:
                    thread.join(timeout=2)
            if any(thread.is_alive() for thread in drains):
                # Preserve a pending record if an uninstrumented descendant
                # retains a pipe beyond our supported child lifetime.
                execution.begin_job_launch(folder, job["key"], job["token"], lease["token"])
                raise VerificationError("review descendants retained output pipes; launch needs recovery", "review_execution_failed")
            safe_to_clear = True
            if errors:
                raise VerificationError(f"review output spool failed: {errors[0]}", "review_execution_failed")
            return None if timed_out else process.returncode, timed_out, counts
        finally:
            if process is not None:
                if process.poll() is None:
                    stop_process(process)
                for thread in drains:
                    thread.join(timeout=2)
                if len(drains) == 2 and not any(thread.is_alive() for thread in drains):
                    safe_to_clear = True
                if not drains:
                    # Registration failed, but the actual child is now reaped.
                    for pipe in (process.stdout, process.stderr):
                        pipe.close()
                    safe_to_clear = True
            if safe_to_clear:
                execution.cancel_job_launch(folder, job["key"], job["token"], lease["token"])
