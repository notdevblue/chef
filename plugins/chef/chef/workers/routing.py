"""Deterministic role routing without a service concurrency limit.

Candidates describe observed backend availability, supported roles and tools.
Quality rows are coordinator-validated aggregates for one exact role/condition,
with samples, successes and rework counts. A worker's own success text is not a
quality observation. Active job counts are tie breakers, never admission caps.
"""

RUNTIMES = ("claude", "codex", "grok")
ROLES = ("researcher", "dev-small", "dev-medium", "verifier", "code-reviewer", "reviewer-small")
ALIASES = {"research": "researcher", "implement": "dev-medium", "implementSmall": "dev-small",
           "verify": "verifier", "review": "code-reviewer", "audit": "code-reviewer",
           "reviewSmall": "reviewer-small"}
ACTIVE = {"reserved", "running"}
MIN_QUALITY_SAMPLES = 5


def known_preference(value):
    """A default/inherited setting cannot identify an observed model stratum."""
    return isinstance(value, str) and bool(value.strip()) and value.strip().casefold() not in (
        "inherit", "unknown", "default", "definition")


def role_name(value):
    if not isinstance(value, str):
        raise ValueError("worker role must be a string")
    value = value.removeprefix("chef:")
    value = ALIASES.get(value, value)
    if value not in ROLES:
        raise ValueError("unsupported Chef worker role")
    return value


def select(candidates, *, role, required_tools=(), jobs=(), quality=(), previous=None,
           independent=False, cursor=0, condition=""):
    """Select an eligible candidate and explain the observed choice.

    A correction's previous runtime is preferred only while still eligible.
    Independent work always requests a fresh context. Quality decides only if
    every eligible candidate has at least five matching validated samples and
    the observed success rates differ by >=20 percentage points or rework by
    >=0.5 attempts per sample. Otherwise load and round-robin decide.
    """
    role = role_name(role)
    if not isinstance(candidates, (list, tuple)):
        raise ValueError("worker candidates must be an array")
    if not isinstance(required_tools, (list, tuple, set)) or any(
            not isinstance(tool, str) or not tool for tool in required_tools):
        raise ValueError("required tools must be nonempty names")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("routing cursor must be a nonnegative integer")
    eligible = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("runtime") not in RUNTIMES:
            raise ValueError("invalid worker candidate")
        runtime = candidate["runtime"]
        if runtime in eligible:
            raise ValueError("duplicate eligible worker runtime")
        roles = candidate.get("roles", ())
        tools = candidate.get("tools", ())
        if not isinstance(roles, (list, tuple)) or not isinstance(tools, (list, tuple)):
            raise ValueError("candidate roles and tools must be arrays")
        if (candidate.get("available") is True and role in {role_name(item) for item in roles}
                and set(required_tools).issubset(tools)):
            eligible[runtime] = candidate
    if not eligible:
        raise ValueError("no available worker satisfies the role and required tools")
    if previous is not None and not independent:
        previous_runtime = previous.get("runtime") if isinstance(previous, dict) else previous
        if previous_runtime in eligible:
            return {"runtime": previous_runtime, "reason": "continue the eligible original worker", "fresh": False}
    if len(eligible) == 1:
        return {"runtime": next(iter(eligible)), "reason": "only candidate with required capabilities", "fresh": True}

    observations = {}
    for row in quality:
        if (not isinstance(row, dict) or row.get("runtime") not in eligible
                or row.get("role") != role or row.get("condition") != condition
                or row.get("policy", "") != eligible[row["runtime"]].get("policy", "")
                or any(not known_preference(eligible[row["runtime"]].get(name))
                       or row.get(name) != eligible[row["runtime"]][name] for name in ("model", "effort"))):
            continue
        samples, successes, rework = row.get("samples"), row.get("successes"), row.get("rework")
        if (type(samples) is int and samples >= MIN_QUALITY_SAMPLES and type(successes) is int
                and 0 <= successes <= samples and type(rework) is int and rework >= 0):
            observations[row["runtime"]] = (successes / samples, rework / samples)
    if len(observations) == len(eligible):
        best = max(observations, key=lambda name: (observations[name][0], -observations[name][1]))
        others = [value for name, value in observations.items() if name != best]
        success, rework = observations[best]
        if all(success - value[0] >= 0.2 - 1e-12 or (
                success >= value[0] and value[1] - rework >= 0.5) for value in others):
            return {"runtime": best, "reason": "matched validated outcomes show lower rework or higher success", "fresh": True}

    loads = dict.fromkeys(eligible, 0)
    for job in jobs:
        runtime = (job.get("route") or {}).get("runtime")
        if job.get("status") in ACTIVE and runtime in loads:
            loads[runtime] += 1
    minimum = min(loads.values())
    tied = {runtime for runtime, load in loads.items() if load == minimum}
    order = RUNTIMES[cursor % len(RUNTIMES):] + RUNTIMES[:cursor % len(RUNTIMES)]
    chosen = next(runtime for runtime in order if runtime in tied)
    return {"runtime": chosen, "reason": "least observed active work; rotating equal candidates", "fresh": True}
