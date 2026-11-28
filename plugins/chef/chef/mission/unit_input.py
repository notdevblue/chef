"""Validated unit input shared by the flag and JSON batch entry points."""

import json
from pathlib import Path

from chef.mission.contracts import BRIEF_FIELDS, NONE_CELL, UNIT_ID, StateError
from chef.mission.storage import clean, normalize_after, parse_timebox


MAX_UNITS = 100
FIELDS = {"id", *BRIEF_FIELDS, "human", "after"}


def template(ids):
    ids = ids or ["M1"]
    if len(ids) > MAX_UNITS or len(set(ids)) != len(ids) or any(not UNIT_ID.fullmatch(i) for i in ids):
        raise StateError("template id: distinct unit ids required (1..100)")
    return {"schema": 1, "units": [
        {"id": i, "goal": "", "files": "", "done": "", "verify": "",
         "timebox": 30, "forbid": "", "human": NONE_CELL, "after": NONE_CELL}
        for i in ids
    ]}


def load(path):
    try:
        source = Path(path)
        if source.stat().st_size > 1024 * 1024:
            raise StateError("unit input exceeds 1 MiB")
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise StateError("unit input: valid UTF-8 JSON required") from error
    return units(data)


def units(data):
    if (not isinstance(data, dict) or set(data) != {"schema", "units"}
            or type(data.get("schema")) is not int or data["schema"] != 1
            or not isinstance(data["units"], list) or not 1 <= len(data["units"]) <= MAX_UNITS):
        raise StateError("unit input: schema=1 and units array (1..100) required")
    return data["units"]


def prepare(items, existing):
    rows = [dict(row) for row in existing]
    additions = []
    errors = []
    for index, item in enumerate(items, 1):
        prefix = f"units[{index}]"
        issues = []
        if not isinstance(item, dict):
            errors.append(f"{prefix}: object required")
            continue
        unknown = set(item) - FIELDS
        if unknown:
            issues.append(f"unknown fields: {', '.join(sorted(unknown))}")
        values = {}
        for name in FIELDS:
            value = item.get(name, NONE_CELL if name in ("human", "after") else "")
            if name == "timebox" and type(value) is int:
                value = str(value)
            if not isinstance(value, str):
                issues.append(f"{name}: text required (timebox also accepts integer minutes)")
                value = ""
            values[name] = clean(value)
        unit_id = values["id"]
        if not UNIT_ID.fullmatch(unit_id):
            issues.append("마일스톤 id 형식 오류 (영문·숫자·._- 만)")
        elif any(row["id"] == unit_id for row in rows):
            issues.append(f"마일스톤 {unit_id} 가 이미 있음 (바꾸려면 unit set)")
        empty = [name for name in BRIEF_FIELDS if not values[name]]
        if empty:
            issues.append(f"브리프 빈 칸: {', '.join(empty)}")
        try:
            values["timebox"] = parse_timebox(values["timebox"])
        except StateError as error:
            issues.append(str(error))
        try:
            values["after"] = normalize_after(values["after"], rows, unit_id)
        except StateError as error:
            issues.append(str(error))
        if issues:
            errors.extend(f"{prefix} ({unit_id or '?'}): {issue}" for issue in issues)
            continue
        row = {**values, "human": values["human"] or NONE_CELL,
               "unit": "?", "started": "", "status": "todo"}
        additions.append(row)
        rows.append(row)
    if errors:
        raise StateError("unit input invalid; no units written: " + " | ".join(errors))
    return additions
