"""verification / plan."""

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from chef.infrastructure.fingerprints import implementation_digest
from chef.verification import references as reference_bundle
from chef.verification.contracts import (
    WINDOWS_RESERVED_NAMES,
    VerificationError,
    digest,
    file_hash,
    read_json,
)


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
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise VerificationError(f"실행 권한 없음: {executable}", "preflight")
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


def reference_identity(project, plan):
    if "referenceBundle" not in plan:
        return {}
    try:
        reference_bundle.require_current(project, plan["referenceBundle"])
        path = reference_bundle.resolve_path(project, plan["referenceBundle"])
        return {
            "path": str(path),
            "sha256": file_hash(path),
            "validator": file_hash(reference_bundle.__file__),
        }
    except (reference_bundle.ReferenceError, OSError) as error:
        raise VerificationError(str(error), "reference_changed") from error


def runtime_key(project, plan, settings=None):
    return digest(
        {
            "verification": implementation_digest(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "environment": declared_environment(plan),
            "referenceBundle": reference_identity(project, plan),
            "verificationPolicy": policy_runtime(project, plan, settings),
            "commands": [command_runtime(project, command) for command in plan["commands"]],
        }
    )


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


def validate_plan(project, plan, grades, require_matches=False):
    if not isinstance(plan.get("grade"), str) or not plan["grade"] or plan["grade"] not in grades:
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
    if (
        not isinstance(keys, list)
        or not all(
            isinstance(name, str)
            and name
            and "=" not in name
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in name)
            for name in keys
        )
        or len(keys) != len(set(keys))
    ):
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
        if (
            not isinstance(result.get("equals", {}), dict)
            or not isinstance(result.get("positive", []), list)
            or not all(isinstance(field, str) and field for field in result.get("positive", []))
        ):
            raise VerificationError("result.equals 는 객체, positive 는 필드 이름 배열이어야 함")
        if not all(isinstance(field, str) and field for field in result.get("equals", {})):
            raise VerificationError("result.equals 는 필드 이름을 키로 써야 함")
        if "projectField" in result:
            if not isinstance(result["projectField"], str) or not result["projectField"]:
                raise VerificationError("result.projectField 는 필드 이름이어야 함")
            project_path(project, result.get("project", "."))
    if plan["grade"] == "editmode-pass" and not any(
        result.get("projectField")
        and result.get("positive")
        and result.get("equals", {}).get("status") == "passed"
        for result in assertions
    ):
        raise VerificationError("editmode-pass 는 status=passed·projectField·양수 테스트 수 검사가 필요함")
    validate_command_inputs(project, plan, snapshot(project, plan.get("inputs")), require_matches)
    return plan


def parse_input_entry(project, value):
    if not isinstance(value, str) or not value:
        raise VerificationError("command.inputs 는 비어 있지 않은 경로·글롭 문자열 배열이어야 함")
    text = value.replace(os.sep, "/")
    while text.startswith("./"):
        text = text[2:]
    if (
        not text
        or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in text)
        or text.startswith("/")
        or re.match(r"[A-Za-z]:", text)
        or ".." in text.split("/")
    ):
        raise VerificationError(f"command.inputs 경로 형식 오류: {value}")
    if any(char in text for char in "*?["):
        return "glob", text
    root = Path(project).resolve()
    return "path", project_path(project, text).relative_to(root).as_posix()


def glob_regex(pattern):
    regex, index = "", 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            regex += "(?:.*/)?"
            index += 3
            continue
        if pattern.startswith("**", index):
            regex += ".*"
            index += 2
            continue
        end = pattern.find("]", index + 2) if char == "[" else -1
        body = pattern[index + 1 : end] if end != -1 else ""
        if char == "*":
            regex += "[^/]*"
        elif char == "?":
            regex += "[^/]"
        elif end != -1 and body.lstrip("!"):
            regex += (
                "["
                + ("^" if body.startswith("!") else "")
                + re.sub(r"([\\^\[])", r"\\\1", body.lstrip("!"))
                + "]"
            )
            index = end
        else:
            regex += re.escape(char)
        index += 1
    return regex


def glob_ignores_case():
    return os.name == "nt"


def input_matcher(kind, text):
    if kind == "glob":
        flags = 0
        if glob_ignores_case():
            flags = re.IGNORECASE
        try:
            pattern = re.compile(glob_regex(text), flags)
        except re.error as error:
            raise VerificationError(f"command.inputs 글롭 형식 오류: {text}: {error}") from error
        return lambda key: pattern.fullmatch(key) is not None
    if text == ".":
        return lambda key: True
    return lambda key: key == text or key.startswith(text + "/")


def step_inputs(project, command, before):
    if "inputs" not in command:
        return before
    matchers = [input_matcher(*parse_input_entry(project, value)) for value in command["inputs"]]
    return {key: value for key, value in before.items() if any(matcher(key) for matcher in matchers)}


def validate_command_inputs(project, plan, files, require_matches=False):
    roots = [project_path(project, value) for value in plan["inputs"]]
    root = Path(project).resolve()
    for command in plan["commands"]:
        if "inputs" not in command:
            continue
        declared = command["inputs"]
        if not isinstance(declared, list) or not declared:
            raise VerificationError("command.inputs 는 비어 있지 않은 경로·글롭 배열이어야 함")
        for value in declared:
            kind, text = parse_input_entry(project, value)
            if kind == "path" and not any((root / text).resolve().is_relative_to(base) for base in roots):
                raise VerificationError(f"command.inputs 는 계획 inputs 안의 경로여야 함: {value}")
            matcher = input_matcher(kind, text)
            if require_matches and not any(matcher(key) for key in files):
                raise VerificationError(f"command.inputs 가 계획 inputs 의 파일과 일치하지 않음: {value}")


def load_plan(project, path, grades):
    try:
        return validate_plan(project, read_json(path), grades)
    except VerificationError as error:
        if error.category == "result_invalid":
            error.category = "plan_invalid"
        raise


def verification_settings(project, plan, settings):
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise VerificationError("verify 설정은 객체여야 함", "preflight")
    resource_map = settings.get("resources", {})
    readiness_map = settings.get("readiness", {})
    if not isinstance(resource_map, dict) or not isinstance(readiness_map, dict):
        raise VerificationError("resources 와 readiness 는 등급별 객체여야 함", "preflight")
    for names in resource_map.values():
        if (
            not isinstance(names, list)
            or not all(
                isinstance(name, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name)
                and name.lower() not in WINDOWS_RESERVED_NAMES
                for name in names
            )
            or len(names) != len({name.casefold() for name in names})
        ):
            raise VerificationError("resources 는 중복 없는 안전한 자원 이름 배열이어야 함", "preflight")
    resources = sorted(resource_map.get(plan.get("grade"), []))
    resource_dir = ""
    value = settings.get("resourceDir", "")
    if resources or "resourceDir" in settings:
        if (
            not isinstance(value, str)
            or not value
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
        ):
            raise VerificationError("resources 에 유효한 resourceDir 경로가 필요함", "preflight")
        candidate = Path(value)
        if os.name == "nt" and any(
            re.search(r'[<>:"|?*]', part)
            or part.endswith((".", " "))
            or part.split(".")[0].lower() in WINDOWS_RESERVED_NAMES
            for part in candidate.parts
            if part not in (candidate.anchor, ".", "..")
        ):
            raise VerificationError("resourceDir 에 사용할 수 없는 경로 이름이 있음", "preflight")
        if candidate.is_absolute():
            root = candidate.resolve()
        else:
            try:
                root = project_path(project, value)
            except VerificationError as error:
                raise VerificationError(str(error), "preflight") from error
        if root.exists() and not root.is_dir():
            raise VerificationError("resourceDir 은 폴더여야 함", "preflight")
        ancestor = root
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
            raise VerificationError("resourceDir 경로를 사용할 수 없음", "preflight")
        resource_dir = str(root)
    for commands in readiness_map.values():
        if not isinstance(commands, list):
            raise VerificationError("readiness 는 명령 배열이어야 함", "preflight")
        for command in commands:
            if not isinstance(command, dict):
                raise VerificationError("readiness 명령은 객체여야 함", "preflight")
            argv = command.get("argv", [])
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(value, str) and value for value in argv)
                or any(any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value) for value in argv)
            ):
                raise VerificationError("readiness.argv 는 제어 문자 없는 문자열 배열이어야 함", "preflight")
            timeout = command.get("timeoutSeconds", 30)
            if type(timeout) is not int or not 1 <= timeout <= 60:
                raise VerificationError("readiness.timeoutSeconds 는 1~60 정수여야 함", "preflight")
            try:
                project_path(project, command.get("cwd", "."))
            except VerificationError as error:
                raise VerificationError(str(error), "preflight") from error
    readiness = []
    for command in readiness_map.get(plan.get("grade"), []):
        readiness.append(
            {
                "argv": [
                    value.replace("{project}", str(Path(project).resolve())) for value in command["argv"]
                ],
                "cwd": command.get("cwd", "."),
                "timeoutSeconds": command.get("timeoutSeconds", 30),
            }
        )
    return {"resourceDir": resource_dir, "resources": resources, "readiness": readiness}


def policy_settings(project, plan, settings=None):
    if settings is None:
        return {}
    configured = verification_settings(project, plan, settings)
    grade = plan.get("grade")
    policy = {}
    if configured["resourceDir"]:
        policy.update(resourceDir=configured["resourceDir"], resources={grade: configured["resources"]})
    if configured["readiness"]:
        policy["readiness"] = {
            grade: [
                {
                    "argv": list(command["argv"]),
                    "cwd": str(project_path(project, command.get("cwd", "."))),
                    "timeoutSeconds": command.get("timeoutSeconds", 30),
                }
                for command in settings["readiness"][grade]
            ]
        }
    return policy


def policy_runtime(project, plan, settings=None):
    policy = policy_settings(project, plan, settings)
    if not policy:
        return {}
    configured = verification_settings(project, plan, policy)
    return {
        "settings": policy,
        "readiness": [command_runtime(project, command) for command in configured["readiness"]],
    }


def guarded_runtime(project, command, guard):
    identity = command_runtime(project, command)
    try:
        argv_guard = getattr(guard, "argv", None)
        variants = [command["argv"], [identity["executable"], *command["argv"][1:]]]
        for argv in variants:
            if callable(argv_guard):
                argv_guard(argv, str(project_path(project, command.get("cwd", "."))))
            else:
                # Preserve external text callback compatibility without using
                # Windows quoting to represent a POSIX process invocation.
                from chef.guards.argv import render

                guard(render(argv))
    except Exception as error:
        raise VerificationError(str(error), "guard_rejected") from error
    return identity


def preflight(project, plan, guard, settings=None, require_matches=False):
    result = {
        "status": "fail",
        "grade": "",
        "commandCount": 0,
        "readinessCount": 0,
        "resources": [],
        "failure": {},
    }
    try:
        if not isinstance(plan, dict):
            raise VerificationError("검증 계획은 객체여야 함", "plan_invalid")
        if isinstance(plan.get("grade"), str):
            result["grade"] = plan["grade"]
        root = str(project)
        if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in root) or not Path(project).is_dir():
            raise VerificationError("유효한 프로젝트 폴더가 필요함", "preflight")
        grades = [plan.get("grade")]
        if isinstance(settings, dict):
            grades = settings.get("grades", grades)
        if not isinstance(grades, list) or not all(isinstance(grade, str) and grade for grade in grades):
            raise VerificationError("verify.grades 는 등급 문자열 배열이어야 함", "plan_invalid")
        validate_plan(project, plan, grades, require_matches)
        reference_identity(project, plan)
        configured = verification_settings(project, plan, settings)
        for command in [*plan["commands"], *configured["readiness"]]:
            guarded_runtime(project, command, guard)
        result.update(
            status="pass",
            commandCount=len(plan["commands"]),
            readinessCount=len(configured["readiness"]),
            resources=configured["resources"],
        )
    except (VerificationError, OSError, ValueError, TypeError, RuntimeError) as error:
        category = getattr(error, "category", "preflight")
        if category == "result_invalid":
            category = "plan_invalid"
        result["failure"] = {"category": category, "message": str(error)}
    return result


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
