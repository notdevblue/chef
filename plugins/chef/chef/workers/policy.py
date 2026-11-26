"""Resolve service boundaries before availability checks or worker admission."""

import os

from chef.infrastructure import user_config
from chef.workers.routing import ROLES, RUNTIMES

MODES = ("auto", "native", *RUNTIMES)


def resolve(request, settings, *, owner_runtime, role):
    global_workers = user_config.read().get("workers", {})
    if not isinstance(global_workers, dict):
        raise ValueError("global workers must be an object")
    layers = (global_workers, settings, request)
    mode = request.get("mode", os.environ.get("CHEF_WORKERS_MODE",
                       settings.get("mode", global_workers.get("mode", "auto"))))
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("worker mode must be auto, native, claude, codex or grok")
    runtimes = list(RUNTIMES)
    rules = {}
    for layer in layers:
        if "runtimes" in layer:
            runtimes = layer["runtimes"]
        if "roleRouting" in layer:
            if not isinstance(layer["roleRouting"], dict):
                raise ValueError("workers.roleRouting must be an object")
            rules.update(layer["roleRouting"])
    if (not isinstance(runtimes, list) or not runtimes
            or any(not isinstance(item, str) or item not in RUNTIMES for item in runtimes)
            or len(set(runtimes)) != len(runtimes)):
        raise ValueError("workers.runtimes must be a nonempty array of unique claude, codex or grok names")
    for name, rule in rules.items():
        if name not in ROLES:
            raise ValueError(f"unsupported workers.roleRouting role: {name}")
        if (not isinstance(rule, dict) or set(rule) != {"runtime"}
                or not isinstance(rule["runtime"], str) or rule["runtime"] not in ("auto", *RUNTIMES)):
            raise ValueError(f"workers.roleRouting.{name} requires runtime auto, claude, codex or grok")
        if rule["runtime"] != "auto" and rule["runtime"] not in runtimes:
            raise ValueError(f"workers.roleRouting.{name} conflicts with workers.runtimes")
    fixed = owner_runtime if mode == "native" else mode
    allowed = list(runtimes)
    if mode != "auto":
        if fixed not in runtimes:
            raise ValueError("worker mode conflicts with workers.runtimes")
        if any(rule["runtime"] not in ("auto", fixed) for rule in rules.values()):
            raise ValueError("worker mode conflicts with workers.roleRouting")
        allowed = [fixed]
    pinned = rules.get(role, {}).get("runtime", "auto")
    if pinned != "auto":
        allowed = [pinned]
    return {"mode": mode, "runtimes": list(runtimes),
            "roleRouting": rules, "allowedRuntimes": allowed}
