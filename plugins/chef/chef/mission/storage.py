"""mission / storage."""

import os
from datetime import datetime

from chef.guards import mission as mission_guard
from chef.infrastructure import paths as infrastructure_paths
from chef.infrastructure.storage import atomic_write_text
from chef.mission.contracts import HEADER_FIELD, NONE_CELL, StateError


def clean(value):
    return mission_guard.CELL_BREAKS.sub(" ", str(value)).strip()


def read_text(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except FileNotFoundError:
        raise StateError(f"파일 없음: {path} (init 이 빠진 템플릿 파일을 채움, 있는 파일은 안 덮음)")


def write_text(path, text):
    atomic_write_text(path, text, creation_mode=0o666)


def read_table(path, header):
    lines = read_text(path).split("\n")
    if tuple(lines[0].rstrip().split("\t")) != header:
        raise StateError(f"{os.path.basename(path)} 헤더가 다름 (기대: {' '.join(header)}): {path}")
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) != len(header):
            raise StateError(
                f"{os.path.basename(path)} {number}행 칸 수 {len(cells)} (기대 {len(header)}): 탭이 빠졌거나 넘친 행을 고칠 것"
            )
        rows.append(dict(zip(header, cells)))
    return rows


def write_table(path, header, rows):
    lines = ["\t".join(header)] + ["\t".join(row[name] for name in header) for row in rows]
    write_text(path, "\n".join(lines) + "\n")


def append_row(path, header, cells):
    # 기존 행은 다시 직렬화하지 않고 본문 끝에만 덧붙임(append only)
    text = read_text(path)
    if not text.endswith("\n"):
        text += "\n"
    write_text(path, text + "\t".join(clean(cell) for cell in cells) + "\n")


def find_row(rows, key, value):
    for row in rows:
        if row[key] == value:
            return row
    return None


def require_unit(rows, unit_id):
    row = find_row(rows, "id", unit_id)
    if row is None:
        known = ", ".join(r["id"] for r in rows) or "없음"
        raise StateError(f"마일스톤 {unit_id} 없음 (있는 것: {known})")
    return row


def require_text(label, value):
    cleaned = clean(value)
    if not cleaned:
        raise StateError(f"{label} 비어 있음")
    return cleaned


def parse_timebox(value):
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise StateError(f"timebox 는 양의 정수(분): {value}")
    return str(int(value))


def normalize_after(value, rows, own_id=""):
    items = list(dict.fromkeys(clean(item) for item in value.split(",") if clean(item)))
    if not items or items == [NONE_CELL]:
        return NONE_CELL
    for item in items:
        if item == own_id:
            raise StateError(f"after 에 자기 자신({item})을 둘 수 없음")
        if find_row(rows, "id", item) is None:
            raise StateError(f"after 의 {item} 는 units.tsv 에 없음 (먼저 unit add, 없으면 {NONE_CELL})")
    return ",".join(items)


def elapsed_minutes(row):
    try:
        started = datetime.fromisoformat(row["started"])
    except ValueError:
        return -1
    if started.tzinfo is None:
        started = started.astimezone()
    return max(0, int((datetime.now().astimezone() - started).total_seconds() // 60))


def is_overdue(row):
    elapsed = elapsed_minutes(row)
    return row["status"] == "doing" and row["timebox"].isdigit() and elapsed > int(row["timebox"])


def ratio_of(row):
    elapsed = elapsed_minutes(row)
    return f"{'?' if elapsed < 0 else elapsed}/{row['timebox']}분"


def progress_of(row):
    if row["status"] != "doing":
        return f"{NONE_CELL}/{row['timebox']}분"
    return f"{ratio_of(row)} 넘김" if is_overdue(row) else ratio_of(row)


def header_end(lines):
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return index
    return len(lines)


def header_fields(lines):
    fields = {}
    for line in lines[: header_end(lines)]:
        match = HEADER_FIELD.match(line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def header_index(lines, key):
    for index in range(header_end(lines)):
        match = HEADER_FIELD.match(lines[index])
        if match and match.group(1) == key:
            return index
    return -1


def stage_and_confirms(fields):
    confirms = ", ".join(f"{key[3:]} {value}" for key, value in fields.items() if key.startswith("확인 "))
    return f"현재 단계 {fields.get('현재 단계', '?')} | 확인 {confirms or '없음'}"


def require_folder(ctx):
    if not os.path.isdir(ctx.folder):
        raise StateError(f"미션 폴더 없음: {ctx.folder} — `init` 먼저")


def poc_relative_path(project, value):
    given = require_text("--poc", value).replace("\\", "/")
    root = os.path.abspath(project)
    target = os.path.abspath(os.path.join(root, given))
    if not infrastructure_paths.inside_root(os.path.realpath(root), os.path.realpath(target)):
        raise StateError(f"--poc 가 작업 공간({root}) 밖: {given}")
    if not os.path.isfile(target):
        raise StateError(f"--poc 파일 없음: {target}")
    return os.path.relpath(target, root).replace(os.sep, "/")
