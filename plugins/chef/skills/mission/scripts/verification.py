import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


RUNNER_VERSION = 2


class VerificationError(Exception):
    def __init__(self, message, category="result_invalid"):
        super().__init__(message)
        self.category = category


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise VerificationError(f"JSON 읽기 실패: {path}: {error}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"JSON 객체가 필요함: {path}")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def executable_identity(path):
    try:
        return file_hash(path)
    except OSError:
        info = Path(path).stat()
        return digest([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


def command_runtime(project, command):
    cwd = project_path(project, command.get("cwd", "."))
    if not cwd.is_dir():
        raise VerificationError(f"명령 cwd 폴더 없음: {cwd}", "preflight")
    name = command["argv"][0]
    local = (cwd / name).resolve()
    executable = local
    if not local.is_file():
        search_path = os.pathsep.join(str((cwd / entry).resolve()) for entry in os.get_exec_path())
        found = ""
        if not Path(name).is_absolute() and Path(name).parent == Path("."):
            found = shutil.which(name, path=os.pathsep.join([str(cwd), search_path])) or ""
        if not found:
            raise VerificationError(f"실행 파일 없음: {name}", "preflight")
        executable = Path(found).resolve()
    if not executable.is_file():
        raise VerificationError(f"실행 파일이 아님: {executable}", "preflight")
    tools = {str(executable): executable_identity(executable)}
    argument = launcher_script(executable, command["argv"][1:])
    if argument:
        candidate = (cwd / argument).resolve()
        if candidate.is_file() and not any(
            candidate.is_relative_to(project_path(project, output)) for output in command.get("outputs", [])
        ):
            tools[str(candidate)] = file_hash(candidate)
    return {"executable": str(executable), "tools": tools}


def launcher_script(executable, arguments):
    name = Path(executable).stem.lower()
    value_options, inline_options = set(), set()
    if name.startswith(("python", "pypy")):
        value_options = {"-X", "-W", "--check-hash-based-pycs"}
        inline_options = {"-c", "-m"}
    elif name in ("node", "nodejs"):
        value_options = {"-r", "--require", "--loader", "--import", "--conditions", "--input-type"}
        inline_options = {"-e", "--eval", "-p", "--print"}
    elif name in ("sh", "bash", "zsh", "dash"):
        value_options = {"-o", "-O"}
        inline_options = {"-c"}
    elif name in ("powershell", "pwsh"):
        value_options = {"-executionpolicy", "-inputformat", "-outputformat", "-workingdirectory"}
        inline_options = {"-command", "-encodedcommand", "-commandwithargs"}
    else:
        return ""
    skip_value, positional = False, False
    for argument in arguments:
        if positional:
            return argument
        token = argument
        if name in ("powershell", "pwsh"):
            token = argument.lower()
        if skip_value:
            skip_value = False
            continue
        if token == "-":
            return ""
        if name.startswith(("python", "pypy")) and token.startswith(("-c", "-m")):
            return ""
        if token in inline_options:
            return ""
        if token in value_options:
            skip_value = True
            continue
        if token == "--":
            positional = True
            continue
        if token.startswith("-"):
            continue
        return argument
    return ""


def declared_environment(plan):
    return {name: os.environ.get(name) for name in plan.get("environmentKeys", [])}


def runtime_key(project, plan):
    return digest({
        "verification": file_hash(__file__), "python": platform.python_version(), "platform": platform.platform(),
        "environment": declared_environment(plan),
        "commands": [command_runtime(project, command) for command in plan["commands"]],
    })


def project_path(project, value):
    if not isinstance(value, str) or not value:
        raise VerificationError("비어 있지 않은 문자열 경로가 필요함")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise VerificationError("경로에 제어 문자를 쓸 수 없음")
    root = Path(project).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise VerificationError(f"프로젝트 밖 경로: {value}")
    return path


def snapshot(project, inputs):
    if not isinstance(inputs, list) or not inputs or not all(isinstance(p, str) and p for p in inputs):
        raise VerificationError("inputs 에 검증에 영향을 주는 파일·폴더를 지정할 것")
    files = {}
    root = Path(project).resolve()
    for value in inputs:
        path = project_path(project, value)
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            resolved = project_path(project, str(candidate))
            if resolved.is_dir():
                continue
            key = candidate.relative_to(root).as_posix()
            files[key] = file_hash(resolved) if resolved.is_file() else "missing"
    if not files or all(value == "missing" for value in files.values()):
        raise VerificationError("검증 inputs 에 실제 파일이 없음")
    return files


def validate_plan(project, plan, grades):
    if plan.get("grade") not in grades:
        raise VerificationError("plan.grade 가 어댑터 verify.grades 에 없음")
    commands = plan.get("commands")
    if not isinstance(commands, list) or not commands:
        raise VerificationError("plan.commands 가 비어 있음")
    for command in commands:
        argv = command.get("argv") if isinstance(command, dict) else []
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
            raise VerificationError("command.argv 는 비어 있지 않은 문자열 배열이어야 함")
        if any(any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value) for value in argv):
            raise VerificationError("command.argv 에 제어 문자를 쓸 수 없음", "preflight")
        try:
            project_path(project, command.get("cwd", "."))
        except VerificationError as error:
            raise VerificationError(str(error), "preflight") from error
        timeout = command.get("timeoutSeconds", 600)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise VerificationError("timeoutSeconds 는 1~3600 정수여야 함")
        if "id" in command and (not isinstance(command["id"], str) or not command["id"].strip()):
            raise VerificationError("command.id 는 비어 있지 않은 문자열이어야 함")
        if "reusable" in command and type(command["reusable"]) is not bool:
            raise VerificationError("command.reusable 은 bool 이어야 함")
        if command.get("reusable") and "outputs" not in command:
            raise VerificationError("재사용 명령은 outputs 배열이 필요함; 순수 검사는 [] 사용")
        outputs = command.get("outputs", [])
        if not isinstance(outputs, list) or not all(isinstance(value, str) and value for value in outputs):
            raise VerificationError("command.outputs 는 경로 배열이어야 함")
        for value in outputs:
            project_path(project, value)
    ids = [command["id"] for command in commands if "id" in command]
    if len(ids) != len(set(ids)):
        raise VerificationError("command.id 가 중복됨")
    if not isinstance(plan.get("environment"), dict) or not plan["environment"]:
        raise VerificationError("plan.environment 에 런타임·대상 환경 식별자를 지정할 것")
    keys = plan.get("environmentKeys", [])
    if (not isinstance(keys, list) or not all(isinstance(name, str) and name and "=" not in name
                                              and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in name)
                                              for name in keys)
            or len(keys) != len(set(keys))):
        raise VerificationError("plan.environmentKeys 는 중복 없는 환경변수 이름 배열이어야 함")
    assertions = plan.get("results", [])
    if not isinstance(assertions, list):
        raise VerificationError("plan.results 는 배열이어야 함")
    for result in assertions:
        if not isinstance(result, dict) or not isinstance(result.get("path"), str):
            raise VerificationError("result.path 가 필요함")
        project_path(project, result["path"])
        if "command" in result:
            producer = result_producer(plan, result)
            if not output_owned(project, commands[producer], result["path"]):
                raise VerificationError("result.path 는 생산 명령의 outputs 안에 있어야 함")
        elif any(command.get("reusable") for command in commands):
            raise VerificationError("단계 재사용 계획의 result.command 가 필요함")
        if (not isinstance(result.get("equals", {}), dict) or not isinstance(result.get("positive", []), list)
                or not all(isinstance(field, str) and field for field in result.get("positive", []))):
            raise VerificationError("result.equals 는 객체, positive 는 필드 이름 배열이어야 함")
        if not all(isinstance(field, str) and field for field in result.get("equals", {})):
            raise VerificationError("result.equals 는 필드 이름을 키로 써야 함")
        if "projectField" in result:
            if not isinstance(result["projectField"], str) or not result["projectField"]:
                raise VerificationError("result.projectField 는 필드 이름이어야 함")
            project_path(project, result.get("project", "."))
    if plan["grade"] == "editmode-pass" and not any(
        result.get("projectField") and result.get("positive") and result.get("equals", {}).get("status") == "passed"
        for result in assertions
    ):
        raise VerificationError("editmode-pass 는 status=passed·projectField·양수 테스트 수 검사가 필요함")
    snapshot(project, plan.get("inputs"))
    return plan


def load_plan(project, path, grades):
    try:
        return validate_plan(project, read_json(path), grades)
    except VerificationError as error:
        if error.category == "result_invalid":
            error.category = "plan_invalid"
        raise


def result_producer(plan, result):
    value = result.get("command")
    if type(value) is int and 0 <= value < len(plan["commands"]):
        return value
    if isinstance(value, str):
        for index, command in enumerate(plan["commands"]):
            if command.get("id") == value:
                return index
    raise VerificationError("result.command 는 명령 index(0부터) 또는 id 여야 함", "plan_invalid")


def output_owned(project, command, result):
    path = project_path(project, result)
    return any(path.is_relative_to(project_path(project, output)) for output in command.get("outputs", []))


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
            if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual) or actual <= 0:
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


def validate_receipt(project, folder, unit, receipt, plan=None):
    if receipt.get("schema") != 1 or receipt.get("status") != "pass" or receipt.get("unit") != unit:
        raise VerificationError("통과한 같은 마일스톤의 검증 기록이 필요함")
    if receipt.get("project") != str(Path(project).resolve()) or receipt.get("mission") != str(Path(folder).resolve()):
        raise VerificationError("다른 프로젝트·미션의 검증 기록임")
    if receipt.get("runnerVersion") != RUNNER_VERSION:
        raise VerificationError("옛 런타임 지문 방식의 검증 기록임: 같은 계획으로 verify 를 다시 실행할 것")
    recorded_plan = receipt.get("plan", {})
    if (not isinstance(recorded_plan, dict) or not isinstance(recorded_plan.get("commands"), list)
            or recorded_plan.get("grade") != receipt.get("grade")):
        raise VerificationError("검증 기록의 계획·등급이 올바르지 않음")
    if plan is not None and recorded_plan != plan:
        raise VerificationError("검증 계획·환경이 바뀜")
    if receipt.get("runner") != runtime_key(project, recorded_plan):
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
            if not isinstance(step, dict) or step.get("index") != index or step.get("status") not in ("passed", "reused"):
                raise VerificationError("통과·재사용되지 않은 검증 단계가 있음")
            validate_step_evidence(project, folder, unit, receipt, step)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    if (receipt.get("schema") != 1 or receipt.get("unit") != unit
            or receipt.get("project") != str(Path(project).resolve())
            or receipt.get("mission") != str(Path(folder).resolve())):
        raise VerificationError("다른 검증 단계의 근거임")
    log = Path(receipt.get("log", ""))
    if not log.is_file() or file_hash(log) != receipt.get("logHash"):
        raise VerificationError("원본 검증 로그가 없거나 바뀜")
    require_cache_integrity(receipt, receipt.get("cacheIntegrity", {}))


def require_cache_integrity(receipt, proof):
    if (not isinstance(proof, dict) or proof.get("status") != "verified" or proof.get("inputsUnchanged") is not True
            or proof.get("runtimeUnchanged") is not True or not proof.get("checkedAt")
            or proof.get("snapshotHash") != digest(receipt.get("snapshot", {}))
            or proof.get("runner") != receipt.get("runner")):
        raise VerificationError("검증 입력·런타임 불변 증거가 없어 단계를 재사용할 수 없음")


def check_run_integrity(project, plan, before, runtime, prior=()):
    inputs_unchanged, runtime_unchanged = False, False
    try:
        inputs_unchanged = before == snapshot(project, plan["inputs"])
    except (VerificationError, OSError):
        pass
    try:
        runtime_unchanged = runtime == runtime_key(project, plan)
    except (VerificationError, OSError):
        pass
    prior = dict(prior)
    if prior.get("status") == "invalid":
        inputs_unchanged = inputs_unchanged and prior.get("inputsUnchanged") is True
        runtime_unchanged = runtime_unchanged and prior.get("runtimeUnchanged") is True
    status = "invalid"
    if inputs_unchanged and runtime_unchanged:
        status = "verified"
    return {"status": status, "inputsUnchanged": inputs_unchanged, "runtimeUnchanged": runtime_unchanged,
            "snapshotHash": digest(before), "runner": runtime, "checkedAt": utc_now()}


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
    owned = [result for result in receipt["plan"].get("results", [])
             if "command" in result and result_producer(receipt["plan"], result) == index]
    if step.get("results", {}) != validate_results(project, {"results": owned}):
        raise VerificationError("검증 단계 결과가 바뀜")
    if step.get("status") == "reused":
        source = Path(step.get("sourceReceipt", ""))
        if not source.is_file() or file_hash(source) != step.get("sourceReceiptHash"):
            raise VerificationError("재사용 원본 검증 기록이 없거나 바뀜")
        original = read_json(source)
        valid_receipt_evidence(project, folder, unit, original)
        source_step = original.get("steps", [])[step["sourceStep"]]
        if (source_step.get("status") != "passed" or source_step.get("key") != step.get("key")
                or source_step.get("outputs") != step.get("outputs") or source_step.get("results") != step.get("results")):
            raise VerificationError("재사용 단계의 원본 증거가 다름")
        validate_step_evidence(project, folder, unit, original, source_step)


def step_key(project, folder, unit, plan, before, index, dependency):
    command = plan["commands"][index]
    settings = {name: value for name, value in plan.items() if name not in ("commands", "results")}
    results = [result for result in plan.get("results", [])
               if "command" in result and result_producer(plan, result) == index]
    return digest({
        "project": str(Path(project).resolve()), "mission": str(Path(folder).resolve()), "unit": unit,
        "settings": settings, "inputs": before, "command": command, "runtime": command_runtime(project, command),
        "runner": file_hash(__file__), "python": platform.python_version(), "platform": platform.platform(),
        "environment": declared_environment(plan),
        "results": results, "dependency": dependency,
    })


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_receipt(project, folder, unit, plan, before, runtime, log_path, receipt_path, started_at,
                 started, steps, category="", error="", previous_path="", previous=None, cache_integrity=()):
    previous = previous or {}
    failed_step = next((step["index"] for step in steps if step["status"] == "failed"), -1)
    signature = ""
    count = 0
    if category:
        signature = digest({"category": category, "step": failed_step, "error": error,
                            "command": plan.get("commands", [])[failed_step:failed_step + 1]})
        count = 1
        if previous.get("failure", {}).get("signature") == signature:
            count += previous["failure"].get("repeatCount", 0)
    results = {}
    if not category:
        try:
            cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity)
            require_run_integrity(cache_integrity)
            results = validate_results(project, plan)
        except VerificationError as failure:
            category, error = failure.category, str(failure)
            signature = digest({"category": category, "step": failed_step, "error": error})
            count = 1
            if previous.get("failure", {}).get("signature") == signature:
                count += previous["failure"].get("repeatCount", 0)
    receipt = {
        "schema": 1, "runnerVersion": RUNNER_VERSION,
        "project": str(Path(project).resolve()), "mission": str(Path(folder).resolve()),
        "unit": unit, "status": "fail" if category else "pass", "grade": plan.get("grade", "unknown"),
        "plan": plan, "snapshot": before, "runner": runtime, "results": results,
        "exitCodes": [step["exitCode"] for step in steps if "exitCode" in step and step["status"] != "reused"],
        "log": str(log_path), "logHash": file_hash(log_path), "seconds": round(time.monotonic() - started, 3),
        "error": error, "startedAt": started_at, "finishedAt": utc_now(), "recordedAt": utc_now(),
        "cacheIntegrity": dict(cache_integrity),
        "steps": steps, "executionCounts": {
            "executed": sum(step["status"] in ("passed", "failed") for step in steps),
            "reused": sum(step["status"] == "reused" for step in steps),
        },
        "failure": {"category": category, "step": failed_step, "signature": signature,
                    "repeatCount": count, "previousReceipt": previous_path},
        "priorReceipt": previous_path, "priorReceiptHash": "",
    }
    if previous_path and Path(previous_path).is_file():
        receipt["priorReceiptHash"] = file_hash(previous_path)
    write_json(receipt_path, receipt)
    write_json(Path(folder) / "verification" / f"{unit}.latest.json", {"receipt": str(receipt_path)})
    return str(receipt_path), receipt, False


def record_failure(project, folder, unit, error, category="plan_invalid"):
    directory = Path(folder) / "verification"
    directory.mkdir(exist_ok=True)
    key = f"{unit}-{uuid.uuid4().hex}"
    log_path = directory / f"{key}.log"
    log_path.write_text(str(error) + "\n", encoding="utf-8")
    previous_path, previous = latest_receipt(folder, unit)
    return save_receipt(project, folder, unit, {}, {}, "", log_path, directory / f"{key}.json",
                        utc_now(), time.monotonic(), [], category, str(error), previous_path, previous)


def run(project, folder, unit, plan, guard, force=False):
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
    try:
        validate_plan(project, plan, [plan.get("grade")])
    except VerificationError as error:
        category = error.category
        if category == "result_invalid":
            category = "plan_invalid"
        return record_failure(project, folder, unit, error, category)
    with log_path.open("wb") as log:
        try:
            runtimes = [command_runtime(project, command) for command in plan["commands"]]
            for command, identity in zip(plan["commands"], runtimes):
                try:
                    guard(subprocess.list2cmdline(command["argv"]))
                    guard(subprocess.list2cmdline([identity["executable"], *command["argv"][1:]]))
                except Exception as error:
                    raise VerificationError(str(error), "guard_rejected") from error
            if previous and not force:
                reusable = False
                try:
                    validate_receipt(project, folder, unit, previous, plan)
                    reusable = True
                except (VerificationError, OSError, TypeError, KeyError, IndexError):
                    pass
                if reusable:
                    log.close()
                    log_path.unlink()
                    return previous_path, previous, True
            before = snapshot(project, plan["inputs"])
            runtime = runtime_key(project, plan)
            prior_results = result_times(project, plan.get("results", []))
            reuse_prefix = not force and bool(previous)
            dependency = ""
            for index, command in enumerate(plan["commands"]):
                step = {"index": index, "id": command.get("id", str(index)), "status": "not_run",
                        "seconds": 0, "startedAt": utc_now(), "finishedAt": "", "outputs": {}, "results": {}}
                steps.append(step)
                step["key"] = step_key(project, folder, unit, plan, before, index, dependency)
                dependency = step["key"]
                if reuse_prefix and command.get("reusable"):
                    try:
                        old = previous.get("steps", [])[index]
                        if (not isinstance(old, dict) or old.get("status") not in ("passed", "reused")
                                or old.get("key") != step["key"]):
                            raise VerificationError("재사용 키가 다름")
                        validate_step_evidence(project, folder, unit, previous, old)
                        step.update(old)
                        step.update(status="reused", startedAt=utc_now(), finishedAt=utc_now(), seconds=0)
                        if old["status"] == "passed":
                            step.update(sourceReceipt=previous_path, sourceReceiptHash=file_hash(previous_path), sourceStep=index)
                        cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity)
                        step["cacheIntegrity"] = dict(cache_integrity)
                        require_run_integrity(cache_integrity)
                        continue
                    except (VerificationError, OSError, KeyError, IndexError, TypeError):
                        pass
                reuse_prefix = False
                step_started = time.monotonic()
                owned = [result for result in plan.get("results", [])
                         if "command" in result and result_producer(plan, result) == index]
                producer_times = result_times(project, owned)
                try:
                    step["status"] = "failed"
                    done = subprocess.run(
                        [runtimes[index]["executable"], *command["argv"][1:]],
                        cwd=project_path(project, command.get("cwd", ".")), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, timeout=command.get("timeoutSeconds", 600), shell=False,
                    )
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
                    cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity)
                    step["cacheIntegrity"] = dict(cache_integrity)
                    if cache_integrity["status"] == "invalid":
                        step["status"] = "failed"
                    require_run_integrity(cache_integrity)
            cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity)
            require_run_integrity(cache_integrity)
            legacy_results = [result for result in plan.get("results", []) if "command" not in result]
            require_fresh_results(project, legacy_results, prior_results)
            validate_results(project, plan)
            for index, step in enumerate(steps):
                if step["outputs"] != output_snapshot(project, plan["commands"][index].get("outputs", [])):
                    raise VerificationError("검증 실행 중 단계 출력이 바뀜", "result_invalid")
                owned = [result for result in plan.get("results", [])
                         if "command" in result and result_producer(plan, result) == index]
                if step["results"] != validate_results(project, {"results": owned}):
                    raise VerificationError("검증 실행 중 단계 결과가 바뀜", "result_invalid")
        except VerificationError as error:
            category, error_text = error.category, str(error)
        except OSError as error:
            category, error_text = "preflight", str(error)
        if before and runtime:
            cache_integrity = check_run_integrity(project, plan, before, runtime, cache_integrity)
            try:
                require_run_integrity(cache_integrity)
            except VerificationError as error:
                category, error_text = error.category, str(error)
        if category:
            log.write((error_text + "\n").encode("utf-8"))
    for index in range(len(steps), len(plan["commands"])):
        steps.append({"index": index, "id": plan["commands"][index].get("id", str(index)),
                      "status": "not_run", "seconds": 0})
    return save_receipt(project, folder, unit, plan, before, runtime, log_path, receipt_path,
                        started_at, started, steps, category, error_text, previous_path, previous, cache_integrity)


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


def review_history(folder, unit):
    latest = Path(folder) / "verification" / f"{unit}.review.latest.json"
    if not latest.exists():
        return []
    history = []
    path = Path(read_json(latest).get("review", ""))
    seen = set()
    while path:
        name = str(path.resolve())
        if name in seen:
            raise VerificationError("리뷰 기록 순환 참조")
        seen.add(name)
        review = read_json(path)
        if review.get("schema") != 1 or review.get("unit") != unit:
            raise VerificationError("다른 단위의 리뷰 기록임")
        evidence = Path(review.get("evidence", ""))
        if not evidence.is_file() or file_hash(evidence) != review.get("evidenceHash"):
            raise VerificationError("이전 리뷰 근거가 없거나 바뀜")
        source = review.get("findingsSource")
        if source and (not Path(source).is_file() or file_hash(source) != review.get("findingsSourceHash")):
            raise VerificationError("이전 리뷰 지적 원본이 없거나 바뀜")
        history.append((name, review))
        previous = review.get("previousReview", "")
        if not previous:
            break
        path = Path(previous)
        if not path.is_file() or file_hash(path) != review.get("previousReviewHash"):
            raise VerificationError("이전 리뷰 연결이 없거나 바뀜")
    return history


def structured_findings(path, history, status, findings_count, fixed_count):
    source = read_json(path)
    if source.get("schema", 1) != 1:
        raise VerificationError("지적 입력 schema 가 다름")
    findings = source.get("findings")
    resolved = source.get("resolvedIds", [])
    suggestions = source.get("suggestions", [])
    if not isinstance(findings, list) or not isinstance(resolved, list) or not isinstance(suggestions, list):
        raise VerificationError("findings·resolvedIds·suggestions 는 배열이어야 함")
    if not all(isinstance(item, str) and item.strip() for item in resolved) or len(resolved) != len(set(resolved)):
        raise VerificationError("resolvedIds 는 중복 없는 id 배열이어야 함")
    known, ever_resolved = {}, set()
    for _, review in reversed(history):
        known.update({finding["id"]: finding for finding in review.get("findings", [])})
        ever_resolved.update(review.get("resolvedIds", []))
    prior_open = {}
    if history:
        prior_open = {finding["id"]: finding for finding in history[0][1].get("findings", [])}
    ids = set()
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str) or not finding["id"].strip():
            raise VerificationError("지적 id 가 필요함")
        identity = finding["id"]
        if identity in ids or identity in resolved:
            raise VerificationError("지적 id 중복 또는 해소·미해소 충돌")
        ids.add(identity)
        if finding.get("kind") not in ("defect", "rule_violation"):
            raise VerificationError("지적 kind 는 defect 또는 rule_violation 이어야 함; 제안은 suggestions 사용")
        if not isinstance(finding.get("reason"), str) or not finding["reason"].strip():
            raise VerificationError("모든 지적에 근거 reason 이 필요함")
        origin = finding.get("origin")
        if origin == "new":
            if identity in known or finding.get("relatedId"):
                raise VerificationError("기존 지적을 new 로 기록할 수 없음")
        elif origin in ("unresolved", "regression"):
            related = finding.get("relatedId", identity)
            if not isinstance(related, str) or related not in known:
                raise VerificationError("반복 지적은 이전 관련 id·이유가 필요함")
            if known[related]["kind"] != finding["kind"]:
                raise VerificationError("같은 지적 id 의 kind 를 바꿀 수 없음")
            if origin == "unresolved" and (identity not in prior_open or related != identity):
                raise VerificationError("unresolved 는 직전 미해소 지적이어야 함")
            if origin == "regression" and (identity in known or related not in ever_resolved or related in prior_open):
                raise VerificationError("regression 은 새 id 와 이전 해소 지적의 relatedId 가 필요함")
        else:
            raise VerificationError("지적 origin 은 new·unresolved·regression 이어야 함")
    if any(identity not in prior_open for identity in resolved):
        raise VerificationError("resolvedIds 는 직전 미해소 지적만 참조할 수 있음")
    if set(prior_open) != (ids & set(prior_open)) | set(resolved):
        raise VerificationError("직전 미해소 지적은 이번 지적 또는 resolvedIds 에 남겨야 함")
    if status == "clean" and findings or status == "findings" and not findings:
        raise VerificationError("리뷰 상태와 구조화된 미해소 지적이 다름")
    if findings_count != -1 and findings_count != len(findings):
        raise VerificationError("findings-count 와 구조화된 지적 수가 다름")
    if fixed_count != -1 and fixed_count != len(resolved):
        raise VerificationError("fixed-count 와 resolvedIds 수가 다름")
    return {"findings": findings, "resolvedIds": resolved, "suggestions": suggestions,
            "findingsCount": len(findings), "fixedCount": len(resolved),
            "newFindingsCount": sum(finding["origin"] == "new" for finding in findings),
            "findingsSource": str(Path(path).resolve()), "findingsSourceHash": file_hash(path)}


def record_review(project, folder, unit, receipt_path, evidence, reviewer, kind, status,
                  findings_count=-1, fixed_count=-1, findings_input=""):
    if findings_count < -1 or fixed_count < -1:
        raise VerificationError("지적·해소 수는 0 이상 또는 미기록(-1)이어야 함")
    if status == "clean" and findings_count > 0 or status == "findings" and findings_count == 0:
        raise VerificationError("리뷰 상태와 남은 지적 수가 다름")
    receipt = read_json(receipt_path)
    validate_receipt(project, folder, unit, receipt)
    validate_latest(folder, unit, receipt_path)
    report = Path(evidence).resolve()
    if not report.is_file() or not reviewer.strip():
        raise VerificationError("독립 리뷰어 이름과 실제 리뷰 결과 파일이 필요함")
    history = review_history(folder, unit)
    if not findings_input and history and history[0][1].get("findings"):
        raise VerificationError("직전 구조화된 미해소 지적은 findings-input 으로 해소·이월해야 함")
    review = {
        "schema": 1, "unit": unit, "kind": kind, "status": status, "reviewer": reviewer,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "findingsCount": findings_count, "fixedCount": fixed_count,
        "receipt": str(Path(receipt_path).resolve()), "receiptHash": file_hash(receipt_path),
        "evidence": str(report), "evidenceHash": file_hash(report),
        "snapshot": receipt["snapshot"], "planHash": digest(receipt["plan"]),
        "round": len(history) + 1, "previousReview": "", "previousReviewHash": "", "diagnostics": [],
    }
    if history:
        review["previousReview"] = history[0][0]
        review["previousReviewHash"] = file_hash(history[0][0])
    if findings_input:
        review.update(structured_findings(findings_input, history, status, findings_count, fixed_count))
    if review["round"] >= 3:
        review["diagnostics"].append("리뷰 3회 이상: 지적 원인·해소 근거·범위와 남은 예산을 확인할 것")
    if any(finding["origin"] in ("unresolved", "regression") for finding in review.get("findings", [])):
        review["diagnostics"].append("반복 지적: 원인과 이전 해소 근거를 다시 확인할 것")
    path = Path(folder) / "verification" / f"{unit}-review-{uuid.uuid4().hex}.json"
    write_json(path, review)
    write_json(Path(folder) / "verification" / f"{unit}.review.latest.json", {"review": str(path)})
    return str(path)


def validate_latest(folder, unit, receipt_path):
    latest = read_json(Path(folder) / "verification" / f"{unit}.latest.json")
    if Path(latest.get("receipt", "")).resolve() != Path(receipt_path).resolve():
        raise VerificationError("마지막 검증 기록이 아님: 이후 실패·재실행 결과를 먼저 확인할 것")


def validate_review(folder, unit, receipt_path, review_path):
    latest = read_json(Path(folder) / "verification" / f"{unit}.review.latest.json")
    if Path(latest.get("review", "")).resolve() != Path(review_path).resolve():
        raise VerificationError("마지막 리뷰 기록이 아님: 이후 리뷰 결과를 먼저 확인할 것")
    review = read_json(review_path)
    if review.get("schema") != 1 or review.get("unit") != unit or review.get("status") != "clean":
        raise VerificationError("같은 마일스톤의 clean 독립 리뷰가 필요함")
    if review.get("receipt") != str(Path(receipt_path).resolve()) or review.get("receiptHash") != file_hash(receipt_path):
        raise VerificationError("리뷰 이후 검증 기록이 바뀜")
    evidence = Path(review.get("evidence", ""))
    if not evidence.is_file() or file_hash(evidence) != review.get("evidenceHash"):
        raise VerificationError("리뷰 근거가 없거나 바뀜")
    if "findings" in review:
        source = Path(review.get("findingsSource", ""))
        if not source.is_file() or file_hash(source) != review.get("findingsSourceHash"):
            raise VerificationError("리뷰 지적 원본이 없거나 바뀜")
        if review["findings"] or review.get("findingsCount") != 0:
            raise VerificationError("clean 리뷰에 미해소 지적이 있음")
    review_history(folder, unit)


def review_scope(project, folder, unit, plan):
    current = snapshot(project, plan["inputs"])
    latest = Path(folder) / "verification" / f"{unit}.review.latest.json"
    prior = {}
    if latest.exists():
        review = read_json(read_json(latest)["review"])
        evidence = Path(review.get("evidence", ""))
        if (review.get("planHash") == digest(plan) and evidence.is_file()
                and file_hash(evidence) == review.get("evidenceHash")):
            prior = review.get("snapshot", {})
    return [name for name in sorted(set(current) | set(prior)) if current.get(name) != prior.get(name)]
