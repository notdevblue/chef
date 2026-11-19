"""verification / receipts."""

import math
import platform
import time
from pathlib import Path

from chef.infrastructure.fingerprints import implementation_digest
from chef.verification.contracts import (
    RUNNER_VERSION,
    VerificationError,
    digest,
    file_hash,
    read_json,
    utc_now,
    write_json,
)
from chef.verification.plan import (
    command_runtime,
    declared_environment,
    policy_runtime,
    policy_settings,
    project_path,
    reference_identity,
    result_producer,
    runtime_key,
    snapshot,
    step_inputs,
)


def output_snapshot(project, outputs):
    state = {}
    root = Path(project).resolve()
    for value in outputs:
        path = project_path(project, value)
        entries = [path]
        if path.is_dir():
            entries.extend(sorted(path.rglob("*")))
        for entry in entries:
            target = project_path(project, str(entry))
            key = entry.relative_to(root).as_posix()
            if target.is_dir():
                state[key] = "directory"
            elif target.is_file():
                state[key] = file_hash(target)
            else:
                state[key] = "missing"
    return state


def result_field(value, name):
    try:
        for part in name.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise VerificationError(f"결과 필드 없음: {name}") from error
    return value


def validate_results(project, plan):
    hashes = {}
    for result in plan.get("results", []):
        path = project_path(project, result["path"])
        value = read_json(path)
        for name, expected in result.get("equals", {}).items():
            if result_field(value, name) != expected:
                raise VerificationError(f"결과 불일치: {result['path']} {name} != {expected}")
        for name in result.get("positive", []):
            actual = result_field(value, name)
            if (
                isinstance(actual, bool)
                or not isinstance(actual, (int, float))
                or not math.isfinite(actual)
                or actual <= 0
            ):
                raise VerificationError(f"실행 수가 양수가 아님: {name}")
        field = result.get("projectField")
        if field:
            actual_path = result_field(value, field)
            if not isinstance(actual_path, str) or not actual_path:
                raise VerificationError(f"결과 프로젝트 경로가 비어 있음: {field}")
            actual = Path(actual_path).resolve()
            expected = project_path(project, result.get("project", "."))
            if actual != expected:
                raise VerificationError(f"검증 프로젝트 불일치: {actual} != {expected}")
        hashes[str(path)] = file_hash(path)
    return hashes


def validate_receipt(project, folder, unit, receipt, plan=None, settings=None):
    if receipt.get("schema") != 1 or receipt.get("status") != "pass" or receipt.get("unit") != unit:
        raise VerificationError("통과한 같은 마일스톤의 검증 기록이 필요함")
    if receipt.get("project") != str(Path(project).resolve()) or receipt.get("mission") != str(
        Path(folder).resolve()
    ):
        raise VerificationError("다른 프로젝트·미션의 검증 기록임")
    if receipt.get("runnerVersion") != RUNNER_VERSION:
        raise VerificationError("옛 런타임 지문 방식의 검증 기록임: 같은 계획으로 verify 를 다시 실행할 것")
    recorded_plan = receipt.get("plan", {})
    if (
        not isinstance(recorded_plan, dict)
        or not isinstance(recorded_plan.get("commands"), list)
        or recorded_plan.get("grade") != receipt.get("grade")
    ):
        raise VerificationError("검증 기록의 계획·등급이 올바르지 않음")
    if plan is not None and recorded_plan != plan:
        raise VerificationError("검증 계획·환경이 바뀜")
    if settings is None:
        settings = receipt.get("verificationPolicy", {})
    if receipt.get("verificationPolicy", {}) != policy_settings(project, recorded_plan, settings):
        raise VerificationError("검증 준비·자원 정책이 바뀜")
    if "grades" in settings and recorded_plan["grade"] not in settings["grades"]:
        raise VerificationError("검증 등급이 현재 어댑터와 다름")
    if receipt.get("runner") != runtime_key(project, recorded_plan, settings):
        raise VerificationError("검증 실행기 런타임이 바뀜")
    if receipt.get("snapshot") != snapshot(project, recorded_plan.get("inputs")):
        raise VerificationError("검증 후 입력 파일·데이터가 바뀜")
    if receipt.get("results") != validate_results(project, recorded_plan):
        raise VerificationError("검증 결과 파일이 바뀜")
    log = Path(receipt.get("log", ""))
    if not log.is_file() or file_hash(log) != receipt.get("logHash"):
        raise VerificationError("검증 로그가 없거나 바뀜")
    if "steps" in receipt:
        steps = receipt["steps"]
        if not isinstance(steps, list) or len(steps) != len(recorded_plan["commands"]):
            raise VerificationError("검증 단계 기록이 누락됨")
        for index, step in enumerate(steps):
            if (
                not isinstance(step, dict)
                or step.get("index") != index
                or step.get("status") not in ("passed", "reused")
            ):
                raise VerificationError("통과·재사용되지 않은 검증 단계가 있음")
            validate_step_evidence(project, folder, unit, receipt, step)


def latest_receipt(folder, unit):
    latest = Path(folder) / "verification" / f"{unit}.latest.json"
    if not latest.exists():
        return "", {}
    try:
        path = Path(read_json(latest).get("receipt", ""))
        receipt = read_json(path)
        return str(path.resolve()), receipt
    except (VerificationError, OSError, TypeError, ValueError):
        return "", {}


def valid_receipt_evidence(project, folder, unit, receipt):
    if (
        receipt.get("schema") != 1
        or receipt.get("unit") != unit
        or receipt.get("project") != str(Path(project).resolve())
        or receipt.get("mission") != str(Path(folder).resolve())
    ):
        raise VerificationError("다른 검증 단계의 근거임")
    log = Path(receipt.get("log", ""))
    if not log.is_file() or file_hash(log) != receipt.get("logHash"):
        raise VerificationError("원본 검증 로그가 없거나 바뀜")
    require_cache_integrity(receipt, receipt.get("cacheIntegrity", {}))


def require_cache_integrity(receipt, proof):
    if (
        not isinstance(proof, dict)
        or proof.get("status") != "verified"
        or proof.get("inputsUnchanged") is not True
        or proof.get("runtimeUnchanged") is not True
        or not proof.get("checkedAt")
        or proof.get("snapshotHash") != digest(receipt.get("snapshot", {}))
        or proof.get("runner") != receipt.get("runner")
    ):
        raise VerificationError("검증 입력·런타임 불변 증거가 없어 단계를 재사용할 수 없음")


def check_run_integrity(project, plan, before, runtime, prior=(), settings=None):
    inputs_unchanged, runtime_unchanged = False, False
    try:
        inputs_unchanged = before == snapshot(project, plan["inputs"])
    except (VerificationError, OSError):
        pass
    try:
        runtime_unchanged = runtime == runtime_key(project, plan, settings)
    except (VerificationError, OSError):
        pass
    prior = dict(prior)
    if prior.get("status") == "invalid":
        inputs_unchanged = inputs_unchanged and prior.get("inputsUnchanged") is True
        runtime_unchanged = runtime_unchanged and prior.get("runtimeUnchanged") is True
    status = "invalid"
    if inputs_unchanged and runtime_unchanged:
        status = "verified"
    return {
        "status": status,
        "inputsUnchanged": inputs_unchanged,
        "runtimeUnchanged": runtime_unchanged,
        "snapshotHash": digest(before),
        "runner": runtime,
        "checkedAt": utc_now(),
    }


def require_run_integrity(proof):
    if proof["inputsUnchanged"] is not True:
        raise VerificationError("검증 실행 중 입력 파일·데이터가 바뀜", "input_changed")
    if proof["runtimeUnchanged"] is not True:
        raise VerificationError("검증 실행 중 런타임이 바뀜", "runtime_changed")


def validate_step_evidence(project, folder, unit, receipt, step):
    valid_receipt_evidence(project, folder, unit, receipt)
    index = step["index"]
    command = receipt["plan"]["commands"][index]
    require_cache_integrity(receipt, step.get("cacheIntegrity", {}))
    if step.get("outputs") != output_snapshot(project, command.get("outputs", [])):
        raise VerificationError("검증 단계 출력이 바뀜")
    owned = [
        result
        for result in receipt["plan"].get("results", [])
        if "command" in result and result_producer(receipt["plan"], result) == index
    ]
    if step.get("results", {}) != validate_results(project, {"results": owned}):
        raise VerificationError("검증 단계 결과가 바뀜")
    if step.get("status") == "reused":
        source = Path(step.get("sourceReceipt", ""))
        if not source.is_file() or file_hash(source) != step.get("sourceReceiptHash"):
            raise VerificationError("재사용 원본 검증 기록이 없거나 바뀜")
        original = read_json(source)
        valid_receipt_evidence(project, folder, unit, original)
        source_step = original.get("steps", [])[step["sourceStep"]]
        if (
            source_step.get("status") != "passed"
            or source_step.get("key") != step.get("key")
            or source_step.get("outputs") != step.get("outputs")
            or source_step.get("results") != step.get("results")
        ):
            raise VerificationError("재사용 단계의 원본 증거가 다름")
        validate_step_evidence(project, folder, unit, original, source_step)


def step_key(project, folder, unit, plan, before, index, dependency, settings=None):
    command = plan["commands"][index]
    plan_settings = {name: value for name, value in plan.items() if name not in ("commands", "results")}
    results = [
        result
        for result in plan.get("results", [])
        if "command" in result and result_producer(plan, result) == index
    ]
    return digest(
        {
            "project": str(Path(project).resolve()),
            "mission": str(Path(folder).resolve()),
            "unit": unit,
            "settings": plan_settings,
            "inputs": step_inputs(project, command, before),
            "command": command,
            "runtime": command_runtime(project, command),
            "runner": implementation_digest(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "environment": declared_environment(plan),
            "referenceBundle": reference_identity(project, plan),
            "verificationPolicy": policy_runtime(project, plan, settings),
            "results": results,
            "dependency": dependency,
        }
    )


def save_receipt(
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
    category="",
    error="",
    previous_path="",
    previous=None,
    cache_integrity=(),
    settings=None,
):
    previous = previous or {}
    failed_step = next((step["index"] for step in steps if step["status"] == "failed"), -1)
    signature = ""
    count = 0
    if category:
        signature = digest(
            {
                "category": category,
                "step": failed_step,
                "error": error,
                "command": plan.get("commands", [])[failed_step : failed_step + 1],
            }
        )
        count = 1
        if previous.get("failure", {}).get("signature") == signature:
            count += previous["failure"].get("repeatCount", 0)
    results = {}
    if not category:
        try:
            cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity, settings)
            require_run_integrity(cache_integrity)
            results = validate_results(project, plan)
        except VerificationError as failure:
            category, error = failure.category, str(failure)
            signature = digest({"category": category, "step": failed_step, "error": error})
            count = 1
            if previous.get("failure", {}).get("signature") == signature:
                count += previous["failure"].get("repeatCount", 0)
    receipt = {
        "schema": 1,
        "runnerVersion": RUNNER_VERSION,
        "project": str(Path(project).resolve()),
        "mission": str(Path(folder).resolve()),
        "unit": unit,
        "status": "fail" if category else "pass",
        "grade": plan.get("grade", "unknown"),
        "plan": plan,
        "snapshot": before,
        "runner": runtime,
        "results": results,
        "exitCodes": [
            step["exitCode"] for step in steps if "exitCode" in step and step["status"] != "reused"
        ],
        "log": str(log_path),
        "logHash": file_hash(log_path),
        "seconds": round(time.monotonic() - started, 3),
        "error": error,
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "recordedAt": utc_now(),
        "cacheIntegrity": dict(cache_integrity),
        "steps": steps,
        "executionCounts": {
            "executed": sum(step["status"] in ("passed", "failed") for step in steps),
            "reused": sum(step["status"] == "reused" for step in steps),
        },
        "failure": {
            "category": category,
            "step": failed_step,
            "signature": signature,
            "repeatCount": count,
            "previousReceipt": previous_path,
        },
        "priorReceipt": previous_path,
        "priorReceiptHash": "",
    }
    if previous_path and Path(previous_path).is_file():
        receipt["priorReceiptHash"] = file_hash(previous_path)
    policy = policy_settings(project, plan, settings)
    if policy:
        receipt["verificationPolicy"] = policy
    write_json(receipt_path, receipt)
    write_json(Path(folder) / "verification" / f"{unit}.latest.json", {"receipt": str(receipt_path)})
    return str(receipt_path), receipt, False


def result_times(project, results):
    values = {}
    for result in results:
        path = project_path(project, result["path"])
        values[result["path"]] = path.stat().st_mtime_ns if path.is_file() else -1
    return values


def require_fresh_results(project, results, prior):
    current = result_times(project, results)
    for name, stamp in current.items():
        if stamp == -1 or stamp == prior[name]:
            raise VerificationError(f"이번 생산 명령에서 결과가 갱신되지 않음: {name}")


def validate_latest(folder, unit, receipt_path):
    latest = read_json(Path(folder) / "verification" / f"{unit}.latest.json")
    if Path(latest.get("receipt", "")).resolve() != Path(receipt_path).resolve():
        raise VerificationError("마지막 검증 기록이 아님: 이후 실패·재실행 결과를 먼저 확인할 것")
