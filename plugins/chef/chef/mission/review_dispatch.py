"""Synchronous, PID-owned review dispatch; completion is not a review verdict."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from chef.infrastructure.fingerprints import implementation_digest
from chef.infrastructure.process import child_env
from chef.locations import PLUGIN_ROOT
from chef.mission.contracts import StateError
from chef.mission import review_worker
from chef.verification import execution, plan as plans, references
from chef.verification.contracts import VerificationError, digest, file_hash

MAX_REQUEST_BYTES = 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024


def request_array(project, value):
    path = references.resolve_path(project, value)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds size limit")
        items = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError("request must be a bounded JSON array")
        return items
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise StateError(f"review request 읽기 실패: {error}") from error


def runtime_identity(project, command, guard):
    runtime = plans.guarded_runtime(project, command, guard)
    # Content hashes are required here; a stat fallback does not pin executable bytes.
    try:
        actual = {path: file_hash(path) for path in runtime["tools"]}
    except OSError as error:
        raise VerificationError("review tool bytes cannot be pinned", "review_tool_unpinned") from error
    if actual != runtime["tools"]:
        raise VerificationError("review tool hash differs", "review_tool_unpinned")
    return runtime


def checked_pointer(folder, pointer):
    if not isinstance(pointer, dict) or set(pointer) != {"path", "sha256"}:
        raise VerificationError("review result pointer is invalid", "review_result_invalid")
    actual = references.file_pointer(folder, pointer["path"])
    if actual != pointer:
        raise VerificationError("review result file changed", "review_result_invalid")
    return references.resolve_path(folder, pointer["path"])


def read_completed(folder, job, request_identity):
    try:
        path = checked_pointer(folder, job.get("result"))
        with path.open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("result exceeds size limit")
        result = json.loads(raw)
        if (not isinstance(result, dict) or result.get("schema") != 1
                or result.get("requestIdentity") != request_identity
                or result.get("status") not in ("completed", "failed")
                or result.get("reviewVerdict") != "not_recorded"):
            raise ValueError("review result identity differs")
        for name in ("stdout", "stderr"):
            checked_pointer(folder, result[name]["file"])
        return result
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        raise VerificationError(f"review result cannot be reused: {error}", "review_result_invalid") from error


def job_summary(job):
    summary = {key: job[key] for key in ("key", "pid", "token")}
    for name in ("launchPending", "child"):
        if name in job:
            summary[name] = job[name]
    return summary


def log_pointer(folder, path, counts):
    return {"file": references.file_pointer(folder, str(path.relative_to(Path(folder)))),
            "bytes": path.stat().st_size, "truncated": counts["seen"] > counts["written"]}


def effective_environment(project, plan):
    """Declared values as observed by both the supervisor and reviewer."""
    environment = child_env(str(project))
    return {name: environment.get(name.upper() if os.name == "nt" else name)
            for name in plan.get("environmentKeys", [])}


def run(project, folder, unit, plan, settings, guard, *, expected_snapshot, purpose, scope, findings,
        argv, plan_path, timeout=600, plan_loader=None, adapter=None, runtime_name="claude"):
    if not isinstance(expected_snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot):
        raise VerificationError("--snapshot must be a SHA256 digest", "plan_invalid")
    references.text_value(purpose, "--purpose")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise VerificationError("--timeout-seconds must be 1..3600", "plan_invalid")
    if not argv or not all(isinstance(value, str) and value for value in argv):
        raise VerificationError("review-run requires command argv after --", "plan_invalid")
    for value in argv:
        references.text_value(value, "review argv")
    command = {"argv": argv, "cwd": "."}
    before = plans.snapshot(project, plan["inputs"])
    if digest(before) != expected_snapshot:
        raise VerificationError("review snapshot is stale", "review_snapshot_changed")
    runtime = runtime_identity(project, command, guard)
    reference = plans.reference_identity(project, plan)
    plan_source = references.file_pointer(project, plan_path, external=True)
    implementation = implementation_digest()
    effective_adapter = adapter or {"verify": settings, "guard": {}}
    request_identity = digest({"project": str(Path(project).resolve()), "plan": plan, "settings": settings,
                               "snapshot": before, "command": command, "runtime": runtime, "references": reference, "planSource": plan_source,
                               "environment": effective_environment(project, plan), "timeout": timeout,
                               "implementation": implementation, "maxLogBytes": MAX_LOG_BYTES,
                               "adapter": effective_adapter, "runtimeName": runtime_name})
    job = execution.reserve_review(folder, unit, request_identity, purpose, scope, findings)
    if not job["acquired"]:
        result = read_completed(folder, job, request_identity) if job["status"] == "completed" else None
        if (plans.snapshot(project, plan["inputs"]) != before
                or runtime_identity(project, command, guard) != runtime
                or plans.reference_identity(project, plan) != reference
                or implementation_digest() != implementation
                or references.file_pointer(project, plan_source["path"], external=True) != plan_source
                or (plan_loader is not None and plan_loader() != plan)):
            raise VerificationError("review request changed during reuse", "review_snapshot_changed")
        return {"schema": 1, "status": job["status"], "job": job_summary(job),
                "dispatched": False, "result": result, "reviewVerdict": "not_recorded"}
    directory = references.resolve_path(folder, f"verification/review-jobs/{job['key']}/{job['token']}")
    directory.mkdir(parents=True, exist_ok=False)
    request = {"schema": 1, "project": str(project), "folder": str(folder), "unit": unit, "plan": plan,
               "settings": settings, "adapter": effective_adapter, "implementation": implementation,
               "runtimeName": runtime_name, "command": command, "argv": argv, "timeout": timeout,
               "before": before, "runtime": runtime, "references": reference, "planSource": plan_source,
               "requestIdentity": request_identity, "job": job, "maxLogBytes": MAX_LOG_BYTES}
    request_file = directory / "request.json"
    request_file.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        # The supervisor claims this token before launching any review command.
        # If this CLI dies before claim and a replacement obtains a new token,
        # the old supervisor cannot dispatch. Once claimed, CLI death leaves a
        # live PID owning both the job and cooperative input leases.
        entry = PLUGIN_ROOT / "skills" / "mission" / "scripts" / "mission_state.py"
        worker = subprocess.Popen([sys.executable, str(entry), "_review-worker", "--input", str(request_file)],
                                  cwd=project, env=child_env(str(project)), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        worker.wait()
        stored = execution.read_record(execution.job_path(execution.job_directory(folder), job["key"]))
        if not stored or stored.get("token") != job["token"] or stored.get("status") != "completed":
            raise VerificationError("review supervisor did not complete this job", "review_execution_failed")
        result = read_completed(folder, stored, request_identity)
        return {"schema": 1, "status": result["status"],
                "job": job_summary(stored),
                "dispatched": True, "result": result, "resultFile": stored["result"],
                "reviewVerdict": "not_recorded"}
    except BaseException:
        current = execution.read_record(execution.job_path(execution.job_directory(folder), job["key"]))
        if (current and current.get("status") == "running" and current.get("token") == job["token"]
                and current.get("pid") == os.getpid()):
            execution.finish_job(folder, job["key"], job["token"], release=True)
        raise


def dispatch(project, folder, unit, plan, guard, command, argv, timeout, before, runtime, reference, request_identity, job, log_limit, *, plan_source):
    directory = references.resolve_path(folder, f"verification/review-jobs/{job['key']}/{job['token']}")
    result_file = directory / "result.json"
    roots = [plans.project_path(project, value) for value in plan["inputs"]]
    roots.extend(Path(value) for value in runtime["tools"])
    roots.append(references.resolve_path(project, plan_source["path"], external=True))
    if "referenceBundle" in plan:
        roots.append(plans.project_path(project, plan["referenceBundle"]))
        bundle = references.load(project, plan["referenceBundle"])
        roots.extend(references.resolve_path(project, pointer["path"], external=True)
                     for pointer in references.read_sources(bundle))
    with execution.input_lease(roots, label=f"review:{unit}") as lease:
        # Recheck under the cooperative lease immediately before dispatch.
        if (plans.snapshot(project, plan["inputs"]) != before or runtime_identity(project, command, guard) != runtime
                or plans.reference_identity(project, plan) != reference
                or references.file_pointer(project, plan_source["path"], external=True) != plan_source):
            raise VerificationError("review inputs changed before dispatch", "review_snapshot_changed")
        stdout, stderr = directory / "stdout.log", directory / "stderr.log"
        exit_code, timed_out, log_counts = review_worker.bounded_process(
            [runtime["executable"], *argv[1:]], project, stdout, stderr, timeout, log_limit,
            folder=folder, job=job, lease=lease)
        unchanged = (plans.snapshot(project, plan["inputs"]) == before
                     and runtime_identity(project, command, guard) == runtime
                     and plans.reference_identity(project, plan) == reference
                     and references.file_pointer(project, plan_source["path"], external=True) == plan_source)
        result = {"schema": 1, "requestIdentity": request_identity,
                  "status": "completed" if exit_code == 0 and unchanged and not timed_out else "failed",
                  "exitCode": exit_code, "timedOut": timed_out, "inputsUnchanged": unchanged,
                  "stdout": log_pointer(folder, stdout, log_counts[0]), "stderr": log_pointer(folder, stderr, log_counts[1]),
                  "reviewVerdict": "not_recorded"}
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pointer = references.file_pointer(folder, str(result_file.relative_to(Path(folder))))
        execution.finish_job(folder, job["key"], job["token"], pointer)
    return {"schema": 1, "status": result["status"], "job": job_summary(job),
            "dispatched": True, "result": result, "resultFile": pointer, "reviewVerdict": "not_recorded"}
