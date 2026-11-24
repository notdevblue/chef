"""verification / runner."""

import contextlib
import difflib
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from chef.infrastructure import locking
from chef.verification import execution
from chef.verification.execution import tracked_subprocess as subprocess
from chef.verification import references as reference_bundle
from chef.verification.contracts import (
    COPY_BUDGET,
    COPY_LIMIT,
    INSPECT_CHARS,
    INSPECT_ITEMS,
    PASSING_REVIEW_STATUSES,
    RUNNER_VERSION,
    SEVERITIES,
    WINDOWS_RESERVED_NAMES,
    ScopeDiffError,
    VerificationError,
    digest,
    file_hash,
    read_json,
    utc_now,
    write_json,
)
from chef.verification.inspection import (
    inspect_artifact,
    inspect_artifact_identity,
    inspect_plan_structure,
    inspect_receipt_structure,
    inspect_strings,
    inspect_type,
)
from chef.verification.plan import (
    command_runtime,
    declared_environment,
    executable_identity,
    glob_ignores_case,
    glob_regex,
    guarded_runtime,
    input_matcher,
    launcher_script,
    load_plan,
    output_owned,
    parse_input_entry,
    policy_runtime,
    policy_settings,
    preflight,
    project_path,
    reference_identity,
    result_producer,
    runtime_key,
    snapshot,
    step_inputs,
    validate_command_inputs,
    validate_plan,
    verification_settings,
)
from chef.verification.receipts import (
    check_run_integrity,
    latest_receipt,
    output_snapshot,
    require_cache_integrity,
    require_fresh_results,
    require_run_integrity,
    result_field,
    result_times,
    save_receipt,
    step_key,
    valid_receipt_evidence,
    validate_latest,
    validate_receipt,
    validate_results,
    validate_step_evidence,
)
from chef.verification.reviews import (
    changed_files,
    copy_snapshot_files,
    copy_store,
    first_origin,
    lowered_severity,
    pending_review_start,
    prior_review,
    read_copy,
    record_review,
    record_review_start,
    require_closed_low,
    review_history,
    review_scope,
    review_scope_diff,
    scope_side,
    store_blob,
    structured_findings,
    text_of,
    validate_review,
    write_scope_diff,
)


class ResourceLock(locking.FileLock):
    def __exit__(self, *args):
        if not getattr(self, "retain_marker", False):
            return super().__exit__(*args)
        try:
            self.release_gate()
        finally:
            self.thread_lock.release()

    def reclaim_if_stale(self):
        try:
            pid, started = self.read_holder()
            self.holder = f"pid {pid}, {started}"
        except FileNotFoundError:
            return True
        except OSError:
            self.holder = "확인할 수 없는 소유자"
        return False


@contextlib.contextmanager
def resource_locks(settings):
    with contextlib.ExitStack() as locks:
        holders = []
        try:
            for name in settings["resources"]:
                path = Path(settings["resourceDir"]) / f"{name}.lock"
                holders.append(locks.enter_context(ResourceLock(str(path), name, timeout=0)))
        except locking.LockBusy as error:
            raise VerificationError(str(error), "resource_busy") from error
        yield holders


def child_environment(cwd=None):
    from chef.infrastructure.process import child_env

    environment = child_env(cwd)
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def execute_command(project, command, identity, log):
    cwd = project_path(project, command.get("cwd", "."))
    return subprocess.run(
        [identity["executable"], *command["argv"][1:]],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        timeout=command.get("timeoutSeconds", 600),
        shell=False,
        env=child_environment(str(cwd)),
    )


def require_readiness(project, commands, guard, log):
    for command in commands:
        identity = guarded_runtime(project, command, guard)
        try:
            done = execute_command(project, command, identity, log)
        except subprocess.TimeoutExpired as error:
            raise VerificationError(str(error), "readiness_timeout") from error
        except OSError as error:
            raise VerificationError(str(error), "readiness_launch_error") from error
        if done.returncode:
            raise VerificationError(f"readiness 종료 코드 {done.returncode}", "readiness_exit")


def record_failure(project, folder, unit, error, category="plan_invalid"):
    directory = Path(folder) / "verification"
    directory.mkdir(exist_ok=True)
    key = f"{unit}-{uuid.uuid4().hex}"
    log_path = directory / f"{key}.log"
    log_path.write_text(str(error) + "\n", encoding="utf-8")
    previous_path, previous = latest_receipt(folder, unit)
    return save_receipt(
        project,
        folder,
        unit,
        {},
        {},
        "",
        log_path,
        directory / f"{key}.json",
        utc_now(),
        time.monotonic(),
        [],
        category,
        str(error),
        previous_path,
        previous,
    )


def run(project, folder, unit, plan, guard, force=False, settings=None, *, unit_lock_held=False):
    try:
        lock = contextlib.nullcontext() if unit_lock_held else execution.unit_execution_lock(folder, unit)
        with lock:
            return _run_owned(project, folder, unit, plan, guard, force, settings)
    except execution.ExecutionBusy as error:
        # An in-flight result is a reference to its owner, never evidence for a
        # changed plan/runtime/input set. No failure receipt overwrites that run.
        error.record["sameRequest"] = False
        try:
            error.record["sameRequest"] = error.record.get("requestIdentity") == run_request_identity(
                project, unit, plan, settings
            )
        except (VerificationError, OSError, TypeError, ValueError, KeyError):
            pass
        raise


def run_request_identity(project, unit, plan, settings):
    return digest({"project": str(Path(project).resolve()), "unit": unit, "plan": plan,
                   "settings": settings, "snapshot": snapshot(project, plan["inputs"]),
                   "runtime": runtime_key(project, plan, settings)})


def _run_owned(project, folder, unit, plan, guard, force, settings):
    try:
        if not isinstance(plan, dict):
            raise VerificationError("검증 계획은 객체여야 함", "plan_invalid")
        configured = verification_settings(project, plan, settings)
        with resource_locks(configured) as resources:
            inputs = plan.get("inputs")
            if not isinstance(inputs, list) or not inputs:
                raise VerificationError("inputs 에 검증에 영향을 주는 파일·폴더를 지정할 것", "plan_invalid")
            targets = [project_path(project, value) for value in inputs]
            with execution.input_lease(targets, label=f"verify:{unit}") as lease:
                checked = preflight(project, plan, guard, settings)
                if checked["status"] == "fail":
                    failure = checked["failure"]
                    return record_failure(project, folder, unit, failure["message"], failure["category"])
                job = execution.reserve_job(
                    folder, "verify", {"unit": unit}, restart_completed=True,
                    request_identity=run_request_identity(project, unit, plan, settings),
                    unit=unit,
                )
                if not job["acquired"]:
                    raise execution.ExecutionBusy(job)
                try:
                    with execution.track_commands(folder, job, lease):
                        result = _run_locked(project, folder, unit, plan, guard, force, configured["readiness"], settings)
                    execution.finish_job(folder, job["key"], job["token"], {
                        "receipt": str(result[0]), "receiptHash": file_hash(result[0]),
                        "status": result[1]["status"], "requestIdentity": job["requestIdentity"],
                    })
                    return result
                except BaseException:
                    # A failed launch or interrupted call must not leave a live
                    # same-process reservation that blocks a later retry.
                    current = execution.active_verification(folder, unit)
                    if current and current["token"] == job["token"]:
                        execution.finish_job(folder, job["key"], job["token"], release=True)
                    raise
                finally:
                    current = execution.active_verification(folder, unit)
                    if current and (current.get("launchPending") is True or execution.child_alive(current)):
                        for holder in resources:
                            holder.retain_marker = True
    except execution.ExecutionBusy:
        raise
    except VerificationError as error:
        return record_failure(project, folder, unit, error, error.category)
    except locking.LockBusy as error:
        return record_failure(project, folder, unit, error, "execution_busy")
    except OSError as error:
        return record_failure(project, folder, unit, error, "preflight")


def _run_locked(project, folder, unit, plan, guard, force, readiness, settings):
    directory = Path(folder) / "verification"
    directory.mkdir(exist_ok=True)
    previous_path, previous = latest_receipt(folder, unit)
    key = f"{unit}-{uuid.uuid4().hex}"
    log_path = directory / f"{key}.log"
    receipt_path = directory / f"{key}.json"
    started_at, started = utc_now(), time.monotonic()
    before, runtime, steps = {}, "", []
    cache_integrity = {}
    category, error_text = "", ""
    with log_path.open("wb") as log:
        try:
            runtimes = [guarded_runtime(project, command, guard) for command in plan["commands"]]
            if readiness:
                before = snapshot(project, plan["inputs"])
                runtime = runtime_key(project, plan, settings)
                require_readiness(project, readiness, guard, log)
                cache_integrity = check_run_integrity(project, plan, before, runtime, settings=settings)
                require_run_integrity(cache_integrity)
            if previous and not force:
                reusable = False
                try:
                    validate_receipt(project, folder, unit, previous, plan, settings=settings or {})
                    reusable = True
                except (VerificationError, OSError, TypeError, KeyError, IndexError):
                    pass
                if reusable:
                    log.close()
                    log_path.unlink()
                    return previous_path, previous, True
            if not before:
                before = snapshot(project, plan["inputs"])
                runtime = runtime_key(project, plan, settings)
            prior_results = result_times(project, plan.get("results", []))
            reuse_prefix = not force and bool(previous)
            dependency = ""
            for index, command in enumerate(plan["commands"]):
                step = {
                    "index": index,
                    "id": command.get("id", str(index)),
                    "status": "not_run",
                    "seconds": 0,
                    "startedAt": utc_now(),
                    "finishedAt": "",
                    "outputs": {},
                    "results": {},
                }
                steps.append(step)
                step["key"] = step_key(project, folder, unit, plan, before, index, dependency, settings)
                dependency = step["key"]
                if reuse_prefix and command.get("reusable"):
                    try:
                        old = previous.get("steps", [])[index]
                        if (
                            not isinstance(old, dict)
                            or old.get("status") not in ("passed", "reused")
                            or old.get("key") != step["key"]
                        ):
                            raise VerificationError("재사용 키가 다름")
                        validate_step_evidence(project, folder, unit, previous, old)
                        step.update(old)
                        step.update(status="reused", startedAt=utc_now(), finishedAt=utc_now(), seconds=0)
                        if old["status"] == "passed":
                            step.update(
                                sourceReceipt=previous_path,
                                sourceReceiptHash=file_hash(previous_path),
                                sourceStep=index,
                            )
                        cache_integrity = check_run_integrity(
                            project, plan, before, runtime, cache_integrity, settings
                        )
                        step["cacheIntegrity"] = dict(cache_integrity)
                        require_run_integrity(cache_integrity)
                        continue
                    except (VerificationError, OSError, KeyError, IndexError, TypeError):
                        pass
                reuse_prefix = False
                step_started = time.monotonic()
                owned = [
                    result
                    for result in plan.get("results", [])
                    if "command" in result and result_producer(plan, result) == index
                ]
                producer_times = result_times(project, owned)
                try:
                    step["status"] = "failed"
                    done = execute_command(project, command, runtimes[index], log)
                    step["exitCode"] = done.returncode
                    if done.returncode:
                        raise VerificationError(f"명령 종료 코드 {done.returncode}", "command_exit")
                    require_fresh_results(project, owned, producer_times)
                    step["results"] = validate_results(project, {"results": owned})
                    step["outputs"] = output_snapshot(project, command.get("outputs", []))
                    if "missing" in step["outputs"].values():
                        raise VerificationError("선언한 검증 단계 출력이 없음")
                    step["status"] = "passed"
                except subprocess.TimeoutExpired as error:
                    raise VerificationError(str(error), "timeout") from error
                except OSError as error:
                    raise VerificationError(str(error), "launch_error") from error
                finally:
                    step["seconds"] = round(time.monotonic() - step_started, 3)
                    step["finishedAt"] = utc_now()
                    cache_integrity = check_run_integrity(
                        project, plan, before, runtime, cache_integrity, settings
                    )
                    step["cacheIntegrity"] = dict(cache_integrity)
                    if cache_integrity["status"] == "invalid":
                        step["status"] = "failed"
                    require_run_integrity(cache_integrity)
            cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity, settings)
            require_run_integrity(cache_integrity)
            legacy_results = [result for result in plan.get("results", []) if "command" not in result]
            require_fresh_results(project, legacy_results, prior_results)
            validate_results(project, plan)
            for index, step in enumerate(steps):
                if step["outputs"] != output_snapshot(project, plan["commands"][index].get("outputs", [])):
                    raise VerificationError("검증 실행 중 단계 출력이 바뀜", "result_invalid")
                owned = [
                    result
                    for result in plan.get("results", [])
                    if "command" in result and result_producer(plan, result) == index
                ]
                if step["results"] != validate_results(project, {"results": owned}):
                    raise VerificationError("검증 실행 중 단계 결과가 바뀜", "result_invalid")
        except VerificationError as error:
            category, error_text = error.category, str(error)
        except OSError as error:
            category, error_text = "preflight", str(error)
        if before and runtime:
            cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity, settings)
            try:
                require_run_integrity(cache_integrity)
            except VerificationError as error:
                category, error_text = error.category, str(error)
        if category:
            log.write((error_text + "\n").encode("utf-8"))
    for index in range(len(steps), len(plan["commands"])):
        steps.append(
            {
                "index": index,
                "id": plan["commands"][index].get("id", str(index)),
                "status": "not_run",
                "seconds": 0,
            }
        )
    return save_receipt(
        project,
        folder,
        unit,
        plan,
        before,
        runtime,
        log_path,
        receipt_path,
        started_at,
        started,
        steps,
        category,
        error_text,
        previous_path,
        previous,
        cache_integrity,
        settings,
    )
