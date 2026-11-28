"""Persist and reuse the plan selected for a version of a mission unit."""

import hashlib
import json
import re

from chef.mission import unit_contracts, unit_scope
from chef.mission.storage import write_text
from chef.verification import references
from chef.verification.contracts import VerificationError, read_json
from chef.verification.plan import validate_plan


def paths(folder, unit, sha=""):
    name = f"{unit}/{sha or 'current'}.json"
    return references.resolve_path(folder, f"verification/plans/{name}")


def current(folder, row, *, required=True, check_brief=True):
    pointer = paths(folder, row["id"])
    if not pointer.exists():
        if required:
            raise VerificationError("등록된 검증 계획 없음: chef plan save 또는 --plan 사용", "plan_invalid")
        return None
    value = read_json(pointer)
    sha = value.get("sha256")
    if (value.get("schema") != 1 or value.get("unit") != row["id"]
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise VerificationError("등록된 검증 계획 포인터가 손상됨", "plan_invalid")
    if check_brief and value.get("unitContract") != unit_binding(folder, row):
        raise VerificationError("단위 브리프가 바뀜: 검증 계획을 확인하고 다시 등록할 것", "plan_invalid")
    return value


def unit_binding(folder, row):
    return unit_contracts.binding(folder, row) or {"version": 1, "sha256": unit_contracts.identity(row)}


def path_for(folder, row, value=None):
    if value is None:
        value = current(folder, row)
    path = paths(folder, row["id"], value["sha256"])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != value["sha256"]:
        raise VerificationError("등록된 검증 계획 파일이 없거나 바뀜", "plan_invalid")
    return str(path)


def template(project, row, grades, grade):
    if grade and grade not in grades:
        raise VerificationError("plan.grade 가 어댑터 verify.grades 에 없음", "plan_invalid")
    return {"grade": grade or (grades[0] if len(grades) == 1 else ""),
            "inputs": unit_scope.files(project, row["files"]),
            "commands": [], "environment": {}, "results": []}


def presets(settings):
    value = settings.get("planPresets", {})
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not name.strip() or not isinstance(plan, dict)
        for name, plan in value.items()
    ):
        raise VerificationError("verify.planPresets must name JSON plans", "plan_invalid")
    return value


def from_preset(project, row, settings, name):
    known = presets(settings)
    if name not in known:
        raise VerificationError("unknown verification preset: use chef plan presets", "plan_invalid")
    plan = dict(known[name])
    if "inputs" not in plan:
        plan["inputs"] = unit_scope.files(project, row["files"])
    return plan


def save(project, folder, row, plan, grades, expected):
    if {"unitBrief", "unitContract"} & plan.keys():
        raise VerificationError("unitBrief·unitContract 는 Chef가 저장된 단위에서 가져옴", "plan_invalid")
    validate_plan(project, plan, grades)
    if "referenceBundle" in plan:
        references.require_current(project, plan["referenceBundle"])
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    sha = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    value = {"schema": 1, "unit": row["id"], "unitContract": unit_binding(folder, row), "sha256": sha}
    previous = current(folder, row, required=False, check_brief=False)
    if previous == value:
        return {**value, "path": path_for(folder, row, value)}
    if (previous or {}).get("sha256") != expected:
        raise VerificationError("등록된 계획이 다름: plan show 의 sha256을 --expect-sha256 으로 지정", "plan_invalid")
    path = paths(folder, row["id"], sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_text(path, encoded)
    resolved = path_for(folder, row, value)
    write_text(paths(folder, row["id"]), json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return {**value, "path": resolved}


def show(folder, row):
    value = current(folder, row, check_brief=False)
    return {**value, "path": path_for(folder, row, value),
            "stale": value["unitContract"] != unit_binding(folder, row)}
