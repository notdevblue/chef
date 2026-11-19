"""verification / inspection."""

import hashlib
import json
import math
import sys
from pathlib import Path

from chef.verification.contracts import INSPECT_CHARS, INSPECT_ITEMS, VerificationError


def inspect_type(value, expected, field):
    if type(value) is not expected:
        raise VerificationError(f"inspect: {field} 형식 오류")
    return value


def inspect_strings(value, field):
    inspect_type(value, list, field)
    if not all(type(item) is str and item for item in value):
        raise VerificationError(f"inspect: {field} 는 문자열 배열이어야 함")
    return value


def inspect_plan_structure(plan, allow_empty=False):
    inspect_type(plan, dict, "plan")
    if allow_empty and not plan:
        return plan
    inspect_artifact_identity(plan, "plan")
    if not inspect_type(plan.get("grade"), str, "plan.grade"):
        raise VerificationError("inspect: plan.grade 가 비어 있음")
    if not inspect_strings(plan.get("inputs"), "plan.inputs"):
        raise VerificationError("inspect: plan.inputs 가 비어 있음")
    if not inspect_type(plan.get("environment"), dict, "plan.environment"):
        raise VerificationError("inspect: plan.environment 가 비어 있음")
    commands = inspect_type(plan.get("commands"), list, "plan.commands")
    if not commands:
        raise VerificationError("inspect: plan.commands 가 비어 있음")
    for command in commands:
        inspect_type(command, dict, "command")
        if not inspect_strings(command.get("argv"), "command.argv"):
            raise VerificationError("inspect: command.argv 가 비어 있음")
        for field in ("id", "cwd"):
            if field in command:
                inspect_type(command[field], str, f"command.{field}")
        if "reusable" in command:
            inspect_type(command["reusable"], bool, "command.reusable")
        for field in ("inputs", "outputs"):
            if field in command:
                inspect_strings(command[field], f"command.{field}")
    if "environmentKeys" in plan:
        inspect_strings(plan["environmentKeys"], "plan.environmentKeys")
    if "referenceBundle" in plan:
        inspect_type(plan["referenceBundle"], str, "plan.referenceBundle")
    for field in ("unit", "startedAt", "finishedAt", "recordedAt"):
        if field in plan:
            inspect_type(plan[field], str, f"plan.{field}")
    for result in inspect_type(plan.get("results", []), list, "plan.results"):
        inspect_type(result, dict, "plan.result")
        inspect_type(result.get("path"), str, "plan.result.path")
    return plan


def inspect_artifact_identity(value, kind):
    if "kind" in value and value["kind"] != kind:
        raise VerificationError("inspect: 지원하지 않거나 일치하지 않는 kind")
    if "schema" in value and (type(value["schema"]) is not int or value["schema"] != 1):
        raise VerificationError("inspect: 지원하지 않는 schema")
    return value


def inspect_receipt_structure(receipt):
    if "schema" not in receipt:
        raise VerificationError("inspect: receipt.schema 가 필요함")
    inspect_artifact_identity(receipt, "receipt")
    if receipt.get("status") not in ("pass", "fail"):
        raise VerificationError("inspect: 지원하지 않는 receipt.status")
    for field in ("unit", "grade", "log", "error"):
        inspect_type(receipt.get(field), str, f"receipt.{field}")
    inspect_plan_structure(receipt.get("plan"), allow_empty=receipt["status"] == "fail")
    for field in ("snapshot", "results"):
        values = inspect_type(receipt.get(field), dict, f"receipt.{field}")
        if not all(type(value) is str for value in values.values()):
            raise VerificationError(f"inspect: receipt.{field} 해시 형식 오류")
    for field in (
        "project",
        "mission",
        "runner",
        "logHash",
        "startedAt",
        "finishedAt",
        "recordedAt",
        "priorReceipt",
        "priorReceiptHash",
    ):
        if field in receipt:
            inspect_type(receipt[field], str, f"receipt.{field}")
    if "runnerVersion" in receipt:
        inspect_type(receipt["runnerVersion"], int, "receipt.runnerVersion")
    if "seconds" in receipt:
        seconds = receipt["seconds"]
        if (
            type(seconds) not in (int, float)
            or seconds < 0
            or seconds > sys.float_info.max
            or not math.isfinite(seconds)
        ):
            raise VerificationError("inspect: receipt.seconds 형식 오류")
    steps = inspect_type(receipt.get("steps", []), list, "receipt.steps")
    commands = receipt["plan"].get("commands", [])
    for index, step in enumerate(steps):
        inspect_type(step, dict, "receipt.step")
        if type(step.get("index")) is not int or step["index"] != index or index >= len(commands):
            raise VerificationError("inspect: receipt.step.index 형식 오류")
        if step.get("status") not in ("passed", "failed", "reused", "not_run"):
            raise VerificationError("inspect: 지원하지 않는 receipt.step.status")
        inspect_type(step.get("id"), str, "receipt.step.id")
        for field in ("sourceReceipt", "sourceReceiptHash"):
            if field in step:
                inspect_type(step[field], str, f"receipt.step.{field}")
        for field in ("exitCode", "sourceStep"):
            if field in step:
                inspect_type(step[field], int, f"receipt.step.{field}")
    if "steps" in receipt and receipt["status"] == "pass":
        if len(steps) != len(commands) or any(step["status"] not in ("passed", "reused") for step in steps):
            raise VerificationError("inspect: pass 와 단계 기록이 일치하지 않음")
    if "executionCounts" in receipt:
        counts = inspect_type(receipt["executionCounts"], dict, "receipt.executionCounts")
        for field in ("executed", "reused"):
            if inspect_type(counts.get(field), int, f"receipt.executionCounts.{field}") < 0:
                raise VerificationError("inspect: 실행 횟수는 음수일 수 없음")
        if "steps" in receipt:
            executed = sum(step["status"] in ("passed", "failed") for step in steps)
            reused = sum(step["status"] == "reused" for step in steps)
            if counts != {"executed": executed, "reused": reused}:
                raise VerificationError("inspect: 실행 횟수와 단계 기록이 일치하지 않음")
    if "failure" in receipt:
        failure = inspect_type(receipt["failure"], dict, "receipt.failure")
        for field in ("category", "signature", "previousReceipt"):
            if field in failure:
                inspect_type(failure[field], str, f"receipt.failure.{field}")
        for field, minimum in (("step", -1), ("repeatCount", 0)):
            if field in failure and inspect_type(failure[field], int, f"receipt.failure.{field}") < minimum:
                raise VerificationError(f"inspect: receipt.failure.{field} 범위 오류")
    return receipt


def inspect_artifact(path, kind):
    if kind not in ("plan", "receipt"):
        raise VerificationError("inspect: 지원하지 않는 kind")
    try:
        content = Path(path).read_bytes()
        value = json.loads(content.decode("utf-8-sig"))
    except OSError as error:
        raise VerificationError("inspect: 파일 읽기 실패") from error
    except (UnicodeError, ValueError, RecursionError) as error:
        raise VerificationError("inspect: JSON 읽기 실패") from error
    inspect_type(value, dict, kind)
    if kind == "plan":
        inspect_plan_structure(value)
        plan = value
    else:
        inspect_receipt_structure(value)
        plan = value["plan"]
    truncated = []

    def text_value(text, field):
        if len(text) > INSPECT_CHARS:
            truncated.append(field)
        return text[:INSPECT_CHARS]

    def fields(source, names, prefix):
        result = {}
        for name in names:
            if name in source:
                item = source[name]
                if type(item) is str:
                    item = text_value(item, f"{prefix}.{name}")
                result[name] = item
        return result

    def limited(items):
        return {"items": items[:INSPECT_ITEMS], "omitted": max(0, len(items) - INSPECT_ITEMS)}

    commands = plan.get("commands", [])
    identity_fields = ("schema", "unit", "grade", "startedAt", "finishedAt", "recordedAt")
    if kind == "receipt":
        identity_fields += ("status", "runnerVersion", "seconds")
    result = {
        "kind": kind,
        "current_validity": "not_checked",
        "source": {
            "path": text_value(str(Path(path).resolve()), "source.path"),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        "identity": fields(value, identity_fields, "identity"),
        "counts": {
            "inputEntries": len(plan.get("inputs", [])),
            "referenceBundles": 0,
            "commands": len(commands),
            "resultChecks": len(plan.get("results", [])),
            "reusableCommands": sum(command.get("reusable", False) for command in commands),
        },
    }
    if plan.get("referenceBundle"):
        result["counts"]["referenceBundles"] = 1
        result["referenceBundle"] = text_value(plan["referenceBundle"], "referenceBundle")
    if kind == "plan":
        result["steps"] = limited(
            [
                fields(command, ("id", "reusable"), f"steps.{index}") | {"index": index}
                for index, command in enumerate(commands[:INSPECT_ITEMS])
            ]
        )
        result["steps"]["omitted"] = max(0, len(commands) - INSPECT_ITEMS)
    else:
        result["counts"].update(snapshotFiles=len(value["snapshot"]), resultFiles=len(value["results"]))
        result["recorded"] = fields(
            value, ("error", "log", "logHash", "priorReceipt", "priorReceiptHash"), "recorded"
        )
        if "executionCounts" in value:
            result["executionCounts"] = fields(
                value["executionCounts"], ("executed", "reused"), "executionCounts"
            )
        if "failure" in value:
            result["failure"] = fields(
                value["failure"],
                ("category", "step", "signature", "repeatCount", "previousReceipt"),
                "failure",
            )
        if "steps" in value:
            steps = value["steps"]
            result["statusCounts"] = {
                status: sum(step["status"] == status for step in steps)
                for status in ("passed", "failed", "reused", "not_run")
            }
            result["steps"] = {
                "items": [
                    fields(
                        step,
                        (
                            "index",
                            "id",
                            "status",
                            "exitCode",
                            "sourceReceipt",
                            "sourceReceiptHash",
                            "sourceStep",
                        ),
                        f"steps.{index}",
                    )
                    for index, step in enumerate(steps[:INSPECT_ITEMS])
                ],
                "omitted": max(0, len(steps) - INSPECT_ITEMS),
            }
            failed = [step for step in steps if step["status"] == "failed"]
            result["failedSteps"] = {
                "items": [
                    fields(step, ("index", "id", "exitCode"), f"failedSteps.{index}")
                    for index, step in enumerate(failed[:INSPECT_ITEMS])
                ],
                "omitted": max(0, len(failed) - INSPECT_ITEMS),
            }
    result["truncatedFields"] = limited(truncated)
    return result
