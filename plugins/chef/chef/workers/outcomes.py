"""Record routing evidence only after the mission's independent completion gate."""

import hashlib
from pathlib import Path

from chef.infrastructure.paths import inside_real_root, same_path
from chef.workers.store import Store


def record_completed_unit(ctx, unit, verified_snapshot, *, store=None):
    """Called after receipt and independent review validation in complete_unit.

    Credit only the latest implementation whose observed output still matches
    disk. Worker text, an exit code, and earlier superseded attempts cannot
    establish a quality sample. An exact repeated completion is idempotent.
    """
    store = store or Store()
    history = store.list(metadata=True)
    jobs = [job for job in history
            if job["status"] == "completed"
            and job["request"].get("mission") == ctx.mission_id
            and job["request"].get("unit") == unit
            and job["request"].get("role") in ("dev-small", "dev-medium")
            and same_path(job["request"].get("project", ""), ctx.project)]
    jobs.sort(key=lambda job: job["createdAt"], reverse=True)
    for job in jobs:
        job = store.get(job["key"], reconcile=False)
        result = job.get("result", {})
        observed = result.get("files")
        route = job["route"]
        if (not result.get("observedModel") or not result.get("observedEffort")
                or result["observedModel"] != route.get("model")
                or result["observedEffort"] != route.get("effort")):
            continue
        if result.get("ok") is not True or not isinstance(observed, dict) or not observed:
            continue
        matches = True
        for name, expected in observed.items():
            path = Path(ctx.project, name)
            if (not inside_real_root(ctx.project, path) or path.is_symlink()
                    or verified_snapshot.get(name) != (expected if expected is not None else "missing")):
                matches = False
                break
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if actual != expected:
                matches = False
                break
        if not matches:
            continue
        attempts = [other for other in history
                    if other["request"].get("mission") == ctx.mission_id
                    and other["request"].get("unit") == unit
                    and same_path(other["request"].get("project", ""), ctx.project)
                    and other["request"].get("role") in ("dev-small", "dev-medium")
                    and other["createdAt"] < job["createdAt"]]
        try:
            store.record_quality(job["key"], job["token"], success=True, rework=len(attempts))
        except ValueError as error:
            if "already recorded" not in str(error):
                raise
        return
