"""A verifier may execute one owner-bound plan without source edit authority."""

import hashlib
from pathlib import Path

from chef.infrastructure.paths import inside_real_root, same_path
from chef.locations import PLUGIN_ROOT
from chef.verification.contracts import read_json

LEGACY = PLUGIN_ROOT / "skills/mission/scripts/mission_state.py"
UNIFIED = PLUGIN_ROOT / "chef_cli.py"
CONTEXT_FLAGS = {"--project", "--mission", "--runtime"}


def control_read(script, arguments):
    if same_path(script, UNIFIED):
        command = tuple(arguments[:2])
        return (
            bool(arguments) and arguments[0] in {"context", "schema", "preflight", "review-open"}
            or command in {("mission", "status"), ("mission", "context"), ("mission", "active"), ("unit", "list"),
                           ("reference", "show"), ("question", "show"), ("question", "check")}
        )
    if not same_path(script, LEGACY):
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
    is_unified = same_path(script, UNIFIED)
    if not is_unified and not same_path(script, LEGACY):
        return False
    values = list(arguments)
    options = []
    if is_unified:
        if len(values) < 4 or values[:2] != ["verify", binding["unit"]]:
            return False
        options = values[2:]
        values = values[:2]
    else:
        while len(values) >= 2 and values[0] in CONTEXT_FLAGS:
            options.extend(values[:2])
            values = values[2:]
        if len(values) != 4 or values[:3] != ["verify", binding["unit"], "--plan"]:
            return False
        options.extend(values[2:])
    if len(options) % 2:
        return False
    flags = options[::2]
    if set(flags) - (CONTEXT_FLAGS | {"--plan"}) or len(set(flags)) != len(flags):
        return False
    supplied = dict(zip(flags, options[1::2]))
    if "--plan" not in supplied or (is_unified and "--runtime" not in supplied):
        return False
    for flag, value in supplied.items():
        if flag == "--project" and not same_path(value, data["cwd"]):
            return False
        if flag == "--mission" and value != data["owner"]["mission"]:
            return False
        if flag == "--runtime" and value != data["owner"]["runtime"]:
            return False
    plan = (Path(data["cwd"]) / supplied["--plan"]).resolve()
    return (same_path(plan, binding["path"]) and plan.is_file()
            and hashlib.sha256(plan.read_bytes()).hexdigest() == binding["sha256"])
