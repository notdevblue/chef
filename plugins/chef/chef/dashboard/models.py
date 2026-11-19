"""dashboard / models."""

import html
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlsplit

from chef.dashboard.contracts import (
    NATURAL_SPLIT,
    RAW_PREFIX,
    READ_ERRORS,
    SUBAGENT_STALE_SECONDS,
)
from chef.infrastructure import paths as infrastructure_paths
from chef.mission import state as mission_state


@dataclass
class Mission:
    slot: str
    id: str
    folder: str
    active: bool
    fields: dict = field(default_factory=dict)
    goal: str = ""
    now: str = ""
    units: list = field(default_factory=list)
    last_activity: float = 0.0
    waiting_input: bool = False
    background: bool = False
    unanswered: list = field(default_factory=list)
    skeletons: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    title: str = ""
    questions: dict = field(default_factory=dict)
    question_issues: dict = field(default_factory=dict)
    archived_questions: list = field(default_factory=list)
    state_updated: float = 0.0
    report_updated: float = 0.0
    stale: str = ""  # ACTIVE 끊김 의심 문구(slots.stale_note)
    location_key: str = ""

    @property
    def key(self):
        return self.location_key or self.slot

    @property
    def state(self):
        return self.fields.get("상태") or "-"

    @property
    def stage(self):
        return self.fields.get("현재 단계") or "-"

    @property
    def confirms(self):
        return {key[3:]: value for key, value in self.fields.items() if key.startswith("확인 ")}

    @property
    def counts(self):
        counts = {name: 0 for name in mission_state.UNIT_STATUSES}
        for row in self.units:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    @property
    def doing(self):
        return [row for row in self.units if row["status"] == "doing"]

    @property
    def remaining(self):
        return [row for row in self.units if row["status"] != "done"]


@dataclass
class Location:
    label: str
    state_dir: str
    row: dict
    active: object
    root: str
    missions: list = field(default_factory=list)
    problem: str = ""
    stale: str = ""
    key: str = ""
    stages: list = field(default_factory=list)
    stale_hours: float = 0


def esc(value):
    return html.escape(str(value), quote=True)


def view_url(path, anchor=""):
    return (
        "/view?path="
        + quote(infrastructure_paths.normalize(path), safe="")
        + (f"#{anchor}" if anchor else "")
    )


def raw_url(path):
    return RAW_PREFIX + quote(infrastructure_paths.normalize(path).replace("\\", "/"), safe="/:")


def natural_key(name):
    return [int(part) if part.isdigit() else part for part in NATURAL_SPLIT.split(name)]


def ago(timestamp):
    if not timestamp:
        return "-"
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return "방금"
    for unit, size in (("일", 86400), ("시간", 3600), ("분", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} 전"
    return "방금"


def section_text(lines, title):
    start = next((index for index, line in enumerate(lines) if line.rstrip() == f"## {title}"), -1)
    if start < 0:
        return ""
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return mission_state.HTML_COMMENT.sub("", "\n".join(body)).strip()


def attempt(errors, label, path, reader, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        return reader(path)
    except READ_ERRORS:
        errors.append(label)
        return fallback


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def fresh(path):
    return time.time() - os.path.getmtime(path) <= SUBAGENT_STALE_SECONDS


def modified_at(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def dated(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "기록 없음"
