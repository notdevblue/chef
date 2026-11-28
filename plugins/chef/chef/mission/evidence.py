"""Optional file-pointer evidence. Hash validity does not establish semantic truth."""

import json
import re

from chef.mission.contracts import StateError
from chef.verification import references

PREFIX = "file-evidence:"
MAX_FILES = 1000
MAX_INPUT_BYTES = 1024 * 1024


def capture(project, value, summary=""):
    try:
        if isinstance(value, dict):
            document = value
        else:
            path = references.resolve_path(project, value)
            with path.open("rb") as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise ValueError("input too large")
            document = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(document, dict) or set(document) != {"schema", "files"}
                or type(document["schema"]) is not int or document["schema"] != 1
                or not isinstance(document["files"], list) or not 1 <= len(document["files"]) <= MAX_FILES):
            raise ValueError("expected schema 1 and nonempty files array")
        pointers = []
        seen = set()
        for item in document["files"]:
            if (not isinstance(item, dict) or not {"path", "kind"}.issubset(item)
                    or set(item) - {"path", "kind", "sha256"}
                    or not isinstance(item["kind"], str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item["kind"])):
                raise ValueError("file evidence requires path and kind")
            pointer = references.file_pointer(project, item["path"])
            if pointer["path"] in seen:
                raise ValueError("duplicate evidence path")
            seen.add(pointer["path"])
            if "sha256" in item and item["sha256"] != pointer["sha256"]:
                raise ValueError("evidence sha256 differs")
            pointers.append({**pointer, "kind": item["kind"]})
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise StateError(f"file evidence 확인 실패: {error}") from error
    return PREFIX + json.dumps({"schema": 1, "summary": summary, "files": pointers},
                               ensure_ascii=False, separators=(",", ":"))


def audit(project, rows, limit=20, unit=""):
    if not 1 <= limit <= 100:
        raise StateError("--limit 은 1..100 이어야 함")
    issues = []
    counts = {"legacyRows": 0, "structuredRows": 0, "checkedFiles": 0,
              "missing": 0, "changed": 0, "invalid": 0}

    def issue(row_number, row, status, path=""):
        counts[status] += 1
        if len(issues) < limit:
            issues.append({"row": row_number, "unit": row["unit"], "status": status, "path": path[:500]})

    for number, row in enumerate(rows, start=2):
        if unit and row["unit"] != unit:
            continue
        text = row["evidence"]
        if not text.startswith(PREFIX):
            counts["legacyRows"] += 1
            continue
        counts["structuredRows"] += 1
        try:
            if len(text) > MAX_INPUT_BYTES:
                raise ValueError("evidence exceeds size limit")
            value = json.loads(text[len(PREFIX):])
            if (not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1
                    or not isinstance(value.get("files"), list) or not 1 <= len(value["files"]) <= MAX_FILES):
                raise ValueError("invalid structured evidence")
        except (ValueError, TypeError, RecursionError):
            issue(number, row, "invalid")
            continue
        for pointer in value["files"]:
            counts["checkedFiles"] += 1
            try:
                if (not isinstance(pointer, dict) or set(pointer) != {"path", "kind", "sha256"}
                        or not isinstance(pointer["kind"], str)
                        or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", pointer["kind"])
                        or not isinstance(pointer["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", pointer["sha256"])):
                    raise ValueError("invalid pointer")
                path = references.resolve_path(project, pointer["path"])
                if not path.is_file():
                    issue(number, row, "missing", pointer["path"])
                elif references.file_pointer(project, pointer["path"])["sha256"] != pointer["sha256"]:
                    issue(number, row, "changed", pointer["path"])
            except (ValueError, OSError, TypeError):
                issue(number, row, "invalid")
    total = counts["missing"] + counts["changed"] + counts["invalid"]
    return {"schema": 1, "status": "fail" if total else "pass", "counts": counts,
            "issues": issues, "omitted": max(0, total - len(issues)),
            "semanticValidity": "not_checked"}
