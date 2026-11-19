"""verification / reviews."""

import difflib
import hashlib
import io
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from chef.verification.contracts import (
    COPY_BUDGET,
    COPY_LIMIT,
    PASSING_REVIEW_STATUSES,
    SEVERITIES,
    ScopeDiffError,
    VerificationError,
    digest,
    file_hash,
    read_json,
    write_json,
)
from chef.verification.plan import project_path, runtime_key, snapshot
from chef.verification.receipts import validate_latest, validate_receipt


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
        start = review.get("reviewStart")
        if start and (not Path(start).is_file() or file_hash(start) != review.get("reviewStartHash")):
            raise VerificationError("이전 리뷰 시작 기록이 없거나 바뀜")
        history.append((name, review))
        previous = review.get("previousReview", "")
        if not previous:
            break
        path = Path(previous)
        if not path.is_file() or file_hash(path) != review.get("previousReviewHash"):
            raise VerificationError("이전 리뷰 연결이 없거나 바뀜")
    return history


def first_origin(history, identity):
    for _, review in reversed(history):
        for finding in review.get("findings", []):
            if finding.get("id") == identity:
                return finding.get("origin")
    return ""


def require_closed_low(history, prior_open, findings, resolved):
    if not history or history[0][1].get("status") != "findings":
        raise VerificationError("closed-low 는 직전 리뷰가 findings 일 때만 기록할 수 있음(연속 닫기 금지)")
    if not prior_open:
        raise VerificationError("closed-low 로 닫을 직전 구조화 지적이 없음")
    if history[0][1].get("inputChangedDuringReview"):
        raise VerificationError(
            "직전 리뷰 중 입력이 바뀌어 closed-low 로 닫을 수 없음: 변경분을 독립 재리뷰할 것"
        )
    for identity, finding in prior_open.items():
        if finding.get("severity") != "low" or first_origin(history, identity) == "regression":
            raise VerificationError(f"closed-low 는 Low 이고 회귀가 아닌 지적만 닫을 수 있음: {identity}")
    if findings or not set(prior_open) <= set(resolved):
        raise VerificationError(
            "closed-low 는 findings 가 비어 있고 resolvedIds 가 직전 지적을 모두 덮어야 함"
        )


def lowered_severity(previous, current):
    before, after = previous.get("severity"), current.get("severity")
    if before not in SEVERITIES:
        return False
    return after not in SEVERITIES or SEVERITIES.index(after) > SEVERITIES.index(before)


def structured_findings(path, history, status, findings_count, fixed_count):
    source = read_json(path)
    if source.get("schema", 1) != 1:
        raise VerificationError("지적 입력 schema 가 다름")
    findings = source.get("findings")
    resolved = source.get("resolvedIds", [])
    suggestions = source.get("suggestions", [])
    if not isinstance(findings, list) or not isinstance(resolved, list) or not isinstance(suggestions, list):
        raise VerificationError("findings·resolvedIds·suggestions 는 배열이어야 함")
    if not all(isinstance(item, str) and item.strip() for item in resolved) or len(resolved) != len(
        set(resolved)
    ):
        raise VerificationError("resolvedIds 는 중복 없는 id 배열이어야 함")
    known, ever_resolved = {}, set()
    for _, review in reversed(history):
        known.update({finding["id"]: finding for finding in review.get("findings", [])})
        ever_resolved.update(review.get("resolvedIds", []))
    prior_open = {}
    if history:
        prior_open = {finding["id"]: finding for finding in history[0][1].get("findings", [])}
    if status == "closed-low":
        require_closed_low(history, prior_open, findings, resolved)
    ids = set()
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or not isinstance(finding.get("id"), str)
            or not finding["id"].strip()
        ):
            raise VerificationError("지적 id 가 필요함")
        identity = finding["id"]
        if identity in ids or identity in resolved:
            raise VerificationError("지적 id 중복 또는 해소·미해소 충돌")
        ids.add(identity)
        if finding.get("kind") not in ("defect", "rule_violation"):
            raise VerificationError(
                "지적 kind 는 defect 또는 rule_violation 이어야 함; 제안은 suggestions 사용"
            )
        if "severity" in finding and finding["severity"] not in SEVERITIES:
            raise VerificationError("지적 severity 는 critical·high·medium·low 이어야 함")
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
            if origin == "unresolved" and lowered_severity(known[related], finding):
                raise VerificationError("같은 지적 id 의 severity 를 낮출 수 없음")
            if origin == "unresolved" and (identity not in prior_open or related != identity):
                raise VerificationError("unresolved 는 직전 미해소 지적이어야 함")
            if origin == "regression" and (
                identity in known or related not in ever_resolved or related in prior_open
            ):
                raise VerificationError("regression 은 새 id 와 이전 해소 지적의 relatedId 가 필요함")
        else:
            raise VerificationError("지적 origin 은 new·unresolved·regression 이어야 함")
    if any(identity not in prior_open for identity in resolved):
        raise VerificationError("resolvedIds 는 직전 미해소 지적만 참조할 수 있음")
    if set(prior_open) != (ids & set(prior_open)) | set(resolved):
        raise VerificationError("직전 미해소 지적은 이번 지적 또는 resolvedIds 에 남겨야 함")
    if status in PASSING_REVIEW_STATUSES and findings or status == "findings" and not findings:
        raise VerificationError("리뷰 상태와 구조화된 미해소 지적이 다름")
    if findings_count != -1 and findings_count != len(findings):
        raise VerificationError("findings-count 와 구조화된 지적 수가 다름")
    if fixed_count != -1 and fixed_count != len(resolved):
        raise VerificationError("fixed-count 와 resolvedIds 수가 다름")
    return {
        "findings": findings,
        "resolvedIds": resolved,
        "suggestions": suggestions,
        "findingsCount": len(findings),
        "fixedCount": len(resolved),
        "newFindingsCount": sum(finding["origin"] == "new" for finding in findings),
        "findingsSource": str(Path(path).resolve()),
        "findingsSourceHash": file_hash(path),
    }


def record_review(
    project,
    folder,
    unit,
    receipt_path,
    evidence,
    reviewer,
    kind,
    status,
    findings_count=-1,
    fixed_count=-1,
    findings_input="",
    settings=None,
):
    if findings_count < -1 or fixed_count < -1:
        raise VerificationError("지적·해소 수는 0 이상 또는 미기록(-1)이어야 함")
    if (
        status in PASSING_REVIEW_STATUSES
        and findings_count > 0
        or status == "findings"
        and findings_count == 0
    ):
        raise VerificationError("리뷰 상태와 남은 지적 수가 다름")
    if status == "closed-low" and not findings_input:
        raise VerificationError("closed-low 는 직전 지적을 해소한 findings-input 이 필요함")
    receipt = read_json(receipt_path)
    validate_receipt(project, folder, unit, receipt, settings=settings)
    validate_latest(folder, unit, receipt_path)
    report = Path(evidence).resolve()
    if not report.is_file() or not reviewer.strip():
        raise VerificationError("독립 리뷰어 이름과 실제 리뷰 결과 파일이 필요함")
    history = review_history(folder, unit)
    if not findings_input and history and history[0][1].get("findings"):
        raise VerificationError("직전 구조화된 미해소 지적은 findings-input 으로 해소·이월해야 함")
    review = {
        "schema": 1,
        "unit": unit,
        "kind": kind,
        "status": status,
        "reviewer": reviewer,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "findingsCount": findings_count,
        "fixedCount": fixed_count,
        "receipt": str(Path(receipt_path).resolve()),
        "receiptHash": file_hash(receipt_path),
        "evidence": str(report),
        "evidenceHash": file_hash(report),
        "snapshot": receipt["snapshot"],
        "planHash": digest(receipt["plan"]),
        "runner": receipt["runner"],
        "round": len(history) + 1,
        "previousReview": "",
        "previousReviewHash": "",
        "diagnostics": [],
    }
    if history:
        review["previousReview"] = history[0][0]
        review["previousReviewHash"] = file_hash(history[0][0])
    if findings_input:
        review.update(structured_findings(findings_input, history, status, findings_count, fixed_count))
    if status == "closed-low":
        review["closedLowIds"] = list(review["resolvedIds"])
    if review["round"] >= 3 and status not in PASSING_REVIEW_STATUSES:
        review["diagnostics"].append(
            "리뷰 3회 이상: 남은 지적이 Low뿐이고 회귀가 아니면 수정 diff를 확인해 closed-low 로 닫고, 아니면 지적 원인·해소 근거·범위를 진단한 뒤 질문할 것"
        )
    if any(finding["origin"] in ("unresolved", "regression") for finding in review.get("findings", [])):
        review["diagnostics"].append("반복 지적: 원인과 이전 해소 근거를 다시 확인할 것")
    name = f"{unit}-review-{uuid.uuid4().hex}"
    directory = Path(folder) / "verification"
    start_path, start = pending_review_start(folder, unit, history)
    if start:
        if start.get("inputs") != receipt["plan"].get("inputs"):
            raise VerificationError(
                "리뷰 시작 기록의 inputs 가 검증 계획과 다름: review-start 를 다시 기록할 것"
            )
        review.update(
            snapshot=start["snapshot"],
            copies=start["copies"],
            reviewStart=start_path,
            reviewStartHash=file_hash(start_path),
        )
        changed = changed_files(start["snapshot"], receipt["snapshot"])
        if changed:
            review["inputChangedDuringReview"] = changed
            review["diagnostics"].append(
                f"리뷰 중 입력 변경 {len(changed)}개({', '.join(changed[:5])}): 리뷰는 시작 시점 기준으로 기록함; "
                "변경분은 다음 review-scope 범위에 들어가며 재리뷰 전에는 complete 할 수 없음"
            )
    else:
        review["copies"] = copy_snapshot_files(project, receipt["snapshot"], copy_store(folder, unit))
    path = directory / f"{name}.json"
    write_json(path, review)
    write_json(directory / f"{unit}.review.latest.json", {"review": str(path)})
    return str(path)


def changed_files(before, after):
    return [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]


def text_of(data):
    if len(data) > COPY_LIMIT or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def copy_store(folder, unit):
    return Path(folder) / "verification" / f"{unit}.copies"


def store_blob(directory, name, data):
    destination = directory / name
    if destination.is_file() and file_hash(destination) == name:
        return
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_snapshot_files(project, files, directory, budget=COPY_BUDGET):
    directory = Path(directory).resolve()
    copied, skipped = [], {}
    used = 0
    for key, expected in files.items():
        if expected == "missing":
            continue
        try:
            source = project_path(project, key)
            if source.stat().st_size > COPY_LIMIT:
                skipped[key] = "large"
                continue
            data = source.read_bytes()
        except OSError:
            skipped[key] = "unreadable"
            continue
        if hashlib.sha256(data).hexdigest() != expected:
            skipped[key] = "changed"
        elif text_of(data) is None:
            skipped[key] = "binary"
        elif used + len(data) > budget:
            skipped[key] = "budget"
        else:
            try:
                store_blob(directory, expected, data)
            except OSError:
                skipped[key] = "unwritable"
                continue
            used += len(data)
            copied.append(key)
    return {"dir": str(directory), "files": copied, "skipped": skipped}


def record_review_start(project, folder, unit, plan):
    files = snapshot(project, plan["inputs"])
    history = review_history(folder, unit)
    directory = Path(folder) / "verification"
    directory.mkdir(exist_ok=True)
    name = f"{unit}-reviewstart-{uuid.uuid4().hex}"
    start = {
        "schema": 1,
        "unit": unit,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "inputs": plan["inputs"],
        "snapshot": files,
        "copies": copy_snapshot_files(project, files, copy_store(folder, unit)),
        "previousReview": history[0][0] if history else "",
    }
    path = directory / f"{name}.json"
    write_json(path, start)
    write_json(directory / f"{unit}.review-start.latest.json", {"reviewStart": str(path)})
    return str(path)


def pending_review_start(folder, unit, history):
    latest = Path(folder) / "verification" / f"{unit}.review-start.latest.json"
    if not latest.exists():
        return "", {}
    path = Path(read_json(latest).get("reviewStart", ""))
    start = read_json(path)
    if start.get("schema") != 1 or start.get("unit") != unit:
        raise VerificationError("다른 단위의 리뷰 시작 기록임")
    if start.get("previousReview", "") != (history[0][0] if history else ""):
        return "", {}
    return str(path.resolve()), start


def validate_review(folder, unit, receipt_path, review_path):
    latest = read_json(Path(folder) / "verification" / f"{unit}.review.latest.json")
    if Path(latest.get("review", "")).resolve() != Path(review_path).resolve():
        raise VerificationError("마지막 리뷰 기록이 아님: 이후 리뷰 결과를 먼저 확인할 것")
    review = read_json(review_path)
    if (
        review.get("schema") != 1
        or review.get("unit") != unit
        or review.get("status") not in PASSING_REVIEW_STATUSES
    ):
        raise VerificationError("같은 마일스톤의 clean 또는 closed-low 독립 리뷰가 필요함")
    if review.get("receipt") != str(Path(receipt_path).resolve()) or review.get("receiptHash") != file_hash(
        receipt_path
    ):
        raise VerificationError("리뷰 이후 검증 기록이 바뀜")
    changed = changed_files(review.get("snapshot", {}), read_json(receipt_path).get("snapshot", {}))
    if changed:
        raise VerificationError(
            f"리뷰 이후 바뀐 파일이 있음({len(changed)}개): {', '.join(changed[:10])}; 변경분을 재리뷰할 것"
        )
    if review["status"] == "closed-low" and "findings" not in review:
        raise VerificationError("closed-low 리뷰에 구조화된 지적 기록이 필요함")
    evidence = Path(review.get("evidence", ""))
    if not evidence.is_file() or file_hash(evidence) != review.get("evidenceHash"):
        raise VerificationError("리뷰 근거가 없거나 바뀜")
    if "findings" in review:
        source = Path(review.get("findingsSource", ""))
        if not source.is_file() or file_hash(source) != review.get("findingsSourceHash"):
            raise VerificationError("리뷰 지적 원본이 없거나 바뀜")
        if review["findings"] or review.get("findingsCount") != 0:
            raise VerificationError(f"{review['status']} 리뷰에 미해소 지적이 있음")
    history = review_history(folder, unit)
    if (
        review["status"] == "closed-low"
        and len(history) > 1
        and history[1][1].get("inputChangedDuringReview")
    ):
        raise VerificationError(
            "직전 리뷰 중 입력이 바뀌어 closed-low 로 닫을 수 없음: 변경분을 독립 재리뷰할 것"
        )


def prior_review(project, folder, unit, plan, settings=None):
    latest = Path(folder) / "verification" / f"{unit}.review.latest.json"
    if latest.exists():
        review = read_json(read_json(latest)["review"])
        evidence = Path(review.get("evidence", ""))
        if (
            review.get("planHash") == digest(plan)
            and review.get("runner") == runtime_key(project, plan, settings)
            and evidence.is_file()
            and file_hash(evidence) == review.get("evidenceHash")
        ):
            return review
    return {}


def review_scope(project, folder, unit, plan, settings=None):
    return review_scope_diff(project, folder, unit, plan, settings, write=False)[0]


def review_scope_diff(project, folder, unit, plan, settings=None, write=True):
    current = snapshot(project, plan["inputs"])
    review = prior_review(project, folder, unit, plan, settings)
    prior = review.get("snapshot", {})
    changed = changed_files(prior, current)
    copies = review.get("copies")
    if not write or not changed or not isinstance(copies, dict):
        return changed, ""
    return changed, write_scope_diff(project, folder, unit, prior, current, copies, changed)


def read_copy(copies, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
        raise VerificationError("리뷰 시점 사본 해시가 올바르지 않음")
    path = Path(copies["dir"]) / expected
    if not path.is_file() or file_hash(path) != expected:
        raise VerificationError("리뷰 시점 사본이 없거나 바뀜")
    return path.read_bytes()


def scope_side(read):
    try:
        text = text_of(read())
    except (VerificationError, OSError) as error:
        return None, str(error)
    if text is None:
        return None, "바이너리·대용량"
    return text, ""


def write_scope_diff(project, folder, unit, prior, current, copies, changed):
    lines = []
    for key in changed:
        old_name, new_name = f"a/{key}", f"b/{key}"
        old, old_reason, new, new_reason = "", "", "", ""
        if prior.get(key, "missing") == "missing":
            old_name = "/dev/null"
        elif key in copies.get("files", []):
            old, old_reason = scope_side(lambda: read_copy(copies, prior[key]))
        else:
            old, old_reason = None, "리뷰 시점 사본 없음"
        if current.get(key, "missing") == "missing":
            new_name = "/dev/null"
        else:
            new, new_reason = scope_side(lambda: project_path(project, key).read_bytes())
        if old is None or new is None:
            lines.append(f"# 비교 불가({old_reason or new_reason}): {key}\n")
            continue
        for line in difflib.unified_diff(
            io.StringIO(old, newline="").readlines(),
            io.StringIO(new, newline="").readlines(),
            fromfile=old_name,
            tofile=new_name,
        ):
            if line.endswith("\n"):
                lines.append(line)
            else:
                lines.append(line + "\n\\ No newline at end of file\n")
    path = Path(folder) / "verification" / f"{unit}-reviewdiff-{uuid.uuid4().hex}.diff"
    try:
        path.write_bytes("".join(lines).encode("utf-8"))
    except OSError as error:
        raise ScopeDiffError(str(error), changed) from error
    return str(path)
