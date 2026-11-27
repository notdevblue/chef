"""A verifier may execute one owner-bound plan without source edit authority."""

import hashlib
from pathlib import Path

from chef.infrastructure.paths import inside_real_root, same_path
from chef.locations import PLUGIN_ROOT
from chef.verification.contracts import read_json


def control_read(script, arguments):
    expected = PLUGIN_ROOT / "skills/mission/scripts/mission_state.py"
    if not same_path(script, expected):
        return False
    values = list(arguments)
    while len(values) >= 2 and values[0] in ("--runtime", "--project", "--mission"):
        values = values[2:]
    return bool(values) and values[0] in {"status", "context", "schema", "unit-list", "reference-show", "preflight"}


def bind(request, owner):
    value = request.get("verificationPlan")
    if value is None:
        return None
    if request["role"] != "verifier" or not isinstance(value, str) or not value:
        raise ValueError("verificationPlan requires a verifier and a plan path")
    path = (Path(request["project"]) / value).resolve()
    folder = Path(owner["state"]) / owner["mission"]
    if not inside_real_root(folder, path) or not path.is_file():
        raise ValueError("verificationPlan must be a file in the owned mission")
    plan = read_json(path)
    if not isinstance(plan, dict) or not isinstance(plan.get("commands"), list) or not plan["commands"]:
        raise ValueError("verificationPlan needs commands")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "unit": request["unit"]}


def permitted(script, arguments, data):
    binding = data.get("owner", {}).get("verification")
    if not binding or data["role"] != "verifier":
        return False
    expected = PLUGIN_ROOT / "skills/mission/scripts/mission_state.py"
    if not same_path(script, expected):
        return False
    values = list(arguments)
    while len(values) >= 2 and values[0] in ("--runtime", "--project", "--mission"):
        flag, value = values[:2]
        if flag == "--project" and not same_path(value, data["cwd"]):
            return False
        if flag == "--mission" and value != data["owner"]["mission"]:
            return False
        if flag == "--runtime" and value != data["owner"]["runtime"]:
            return False
        values = values[2:]
    if len(values) != 4 or values[:3] != ["verify", binding["unit"], "--plan"]:
        return False
    plan = (Path(data["cwd"]) / values[3]).resolve()
    return (same_path(plan, binding["path"]) and plan.is_file()
            and hashlib.sha256(plan.read_bytes()).hexdigest() == binding["sha256"])
