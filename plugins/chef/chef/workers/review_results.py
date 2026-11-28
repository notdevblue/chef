"""Bind a structured review result to the verified inputs and review history."""

import json
import re
from pathlib import Path

from chef.infrastructure.paths import inside_real_root, same_path
from chef.mission import managed_plans, unit_contracts
from chef.mission.contracts import EDITABLE_FIELDS, UNIT_HEADER, UNITS_FILE
from chef.mission.storage import read_table, require_unit, write_text
from chef.verification import receipts, references, reviews
from chef.verification.contracts import VerificationError, file_hash, read_json

REVIEW_ROLES = {"code-reviewer", "reviewer-small"}
KINDS = ("poc", "unit", "integration")
OUTPUT = (
    'Return one JSON object: {"schema":1,"report":"nonempty review report",'
    '"findings":[],"resolvedIds":[],"suggestions":[]}. Each finding needs id, kind '
    '(defect or rule_violation), origin (new, unresolved or regression), severity '
    '(critical, high, medium or low), reason, and evidence (path and location). '
    'Use relatedId for regressions. Carry forward existing open IDs or confirm each '
    'resolution in resolvedIds. Read the supplied receipt and previous review if present.'
)


def bind(project, folder, row, settings, kind, path=None):
    if kind not in KINDS:
        raise VerificationError("invalid review kind")
    latest, _ = receipts.latest_receipt(folder, row["id"])
    if not latest:
        raise VerificationError("run verify successfully before dispatching a review")
    path = (Path(project) / path).resolve() if path else Path(latest).resolve()
    directory = Path(folder) / "verification"
    if not inside_real_root(folder, directory) or not inside_real_root(directory, path):
        raise VerificationError("review receipt must stay inside the owned mission verification directory")
    receipts.validate_latest(folder, row["id"], path)
    before = file_hash(path)
    value = read_json(path)
    receipts.validate_receipt(project, folder, row["id"], value, settings=settings)
    plan = value["plan"]
    if (plan.get("unitBrief") != {name: row[name] for name in EDITABLE_FIELDS}
            or plan.get("unitContract") != unit_contracts.binding(folder, row)):
        raise VerificationError("verification receipt does not match the current unit brief")
    if file_hash(path) != before:
        raise VerificationError("verification receipt changed while being read")
    history = reviews.review_history(folder, row["id"])
    previous = history[0][0] if history else ""
    selected = managed_plans.current(folder, row, required=False)
    if selected is not None:
        registered_plan = read_json(managed_plans.path_for(folder, row, selected))
        if registered_plan != {key: item for key, item in plan.items() if key not in ("unitBrief", "unitContract")}:
            raise VerificationError("verify the currently registered plan before dispatching a review")
    return {"kind": kind, "receipt": str(path), "receiptHash": before,
            "previousReview": previous, "previousReviewHash": file_hash(previous) if previous else "",
            "planHash": selected["sha256"] if selected else ""}, plan["inputs"]


def prepare(request, owner, settings, prompt):
    kind = request.pop("reviewKind", None)
    path = request.pop("reviewReceipt", None)
    if "reviewBinding" in request or (path is not None and kind is None):
        raise VerificationError("use reviewKind and an optional reviewReceipt to bind a review")
    if kind is None:
        return prompt
    if request["role"] not in REVIEW_ROLES or (path is not None and not isinstance(path, str)):
        raise VerificationError("a bound review requires a review role and an optional receipt path")
    if request.get("previous") or request.get("files"):
        raise VerificationError("a bound review must be independent and cannot have write files")
    folder = Path(owner["state"]) / owner["mission"]
    row = require_unit(read_table(folder / UNITS_FILE, UNIT_HEADER), request["unit"])
    binding, inputs = bind(request["project"], folder, row, settings, kind, path)
    request.update(independent=True, reviewBinding=binding, inputPaths=[*request.get("inputPaths", []), *inputs])
    return (f"{prompt}\n\n{OUTPUT}\nVerified receipt: {binding['receipt']}\n"
            f"Previous review: {binding['previousReview'] or 'none'}")


def payload(result):
    value = result.get("review", result.get("result"))
    if isinstance(value, str):
        fenced = re.fullmatch(r"\s*`{3}(?:json)?[ \t]*\r?\n(.*?)\r?\n`{3}\s*", value, re.DOTALL | re.IGNORECASE)
        if fenced:
            value = fenced[1]
        try:
            value = json.loads(value)
        except ValueError as error:
            raise VerificationError("reviewer must return a structured JSON result") from error
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or not isinstance(value.get("report"), str) or not value["report"].strip()
            or not all(isinstance(value.get(field), list) for field in ("findings", "resolvedIds", "suggestions"))):
        raise VerificationError("review result requires schema, report and findings/resolvedIds/suggestions arrays")
    return {field: value[field] for field in ("schema", "report", "findings", "resolvedIds", "suggestions")}


def record(project, folder, row, settings, runtime, session, key, store):
    from chef.workers import controller

    job = controller.require_record(store, key)
    request = job["request"]
    expected = {"mission": Path(folder).name, "unit": row["id"], "ownerRuntime": runtime, "ownerSession": session}
    if (not session or not same_path(request["project"], project)
            or any(request.get(field) != value for field, value in expected.items())):
        raise VerificationError("review job does not belong to this project, mission, unit and owner session")
    adapter, owner = controller.ownership(request)
    if (not same_path(Path(owner["state"]) / owner["mission"], folder)
            or controller.digest(adapter) != request["adapterDigest"]):
        raise VerificationError("review job adapter or mission state changed")
    binding = request.get("reviewBinding")
    result = job.get("result") or {}
    if (request["role"] not in REVIEW_ROLES or not binding or job["status"] != "completed"
            or result.get("ok") is not True or result.get("after") != request["before"]):
        raise VerificationError("a successful bound read-only review job is required")
    if controller.input_snapshot(project, request["inputPaths"]) != request["before"]:
        raise VerificationError("review job inputs changed since dispatch")
    current, _ = bind(project, folder, row, settings, binding["kind"], binding["receipt"])
    history = reviews.review_history(folder, row["id"])
    if history and history[0][1].get("workerJob") == key and history[0][1].get("receiptHash") == current["receiptHash"]:
        return history[0][0]
    if current != binding:
        raise VerificationError("review job receipt, registered plan, or prior review changed since dispatch")
    value = payload(result)
    report = value.pop("report")
    status = "findings" if value["findings"] else "clean"
    reviews.structured_findings(value, history, status, -1, -1, require_structured=True)
    proof = references.resolve_path(folder, f"verification/{row['id']}-worker-{controller.digest(report)}.md")
    if not proof.exists():
        write_text(proof, report)
    if proof.read_bytes() != report.encode("utf-8"):
        raise VerificationError("stored worker report changed")
    return reviews.record_review(project, folder, row["id"], binding["receipt"], proof,
                                 f"{job['route']['runtime']}/{request['actualRole']}", binding["kind"], status,
                                 findings_input=value, settings=settings, require_structured=True, worker_job=key)
