"""dashboard / data."""

import copy
import functools
import json
import os
import threading
import time
from contextvars import ContextVar
from pathlib import Path

from chef.dashboard import discovery as dashboard_discovery
from chef.dashboard import access as workspace_access
from chef.dashboard.contracts import (
    ACTIVITY_TYPES,
    CODEX_ROLLOUT,
    CODEX_TURN_DONE,
    CODEX_TURN_EVENTS,
    HANDBACK_TOOL,
    INSPECTION_SETTLE_NS,
    NO_INSPECTION,
    QUESTION_RECORD_HEADER,
    READ_ERRORS,
    RUNNING,
    SKELETON_HEADING,
    SUBAGENT_FILE,
    SUBAGENT_STALE_SECONDS,
    TASK_NOTIFICATION,
    TRANSCRIPT_TAIL_BYTES,
)
from chef.dashboard.documents import read_shared_text
from chef.dashboard.models import (
    Location,
    Mission,
    attempt,
    fresh,
    modified_at,
    natural_key,
    parse_time,
    section_text,
)
from chef.guards import mission as mission_guard
from chef.infrastructure import paths as infrastructure_paths
from chef.mission import state as mission_state
from chef.questions import records as question_records
from chef.runtimes.grok import transcript as grok_transcript

INSPECTION_LIMIT = 2048


INSPECTIONS = {}


INSPECTIONS_LOCK = threading.Lock()
CODEX_CHILDREN = ContextVar("dashboard_codex_children", default=None)
GROK_CHILDREN = {}
GROK_CHILDREN_LOCK = threading.Lock()
GROK_LIFECYCLE_MARKERS = (b"subagent_spawned", b"subagent_finished")


def request_scoped(function):
    @functools.wraps(function)
    def scoped(*args, **kwargs):
        token = CODEX_CHILDREN.set({}) if CODEX_CHILDREN.get() is None else None
        try:
            with infrastructure_paths.request_realpaths():
                return function(*args, **kwargs)
        finally:
            if token is not None:
                CODEX_CHILDREN.reset(token)

    return scoped


def inspect_question(folder, name, text):
    try:
        key, newest = question_records.inspection_stamp(folder, name, text)
    except question_records.QuestionRecordError:
        return question_records.inspect_document(folder, name, text)
    document = (key[0], key[2])
    previous = INSPECTIONS.get(document, NO_INSPECTION)
    if previous[0] == key:
        return copy.deepcopy(previous[1])
    result = question_records.inspect_document(folder, name, text)
    # mtime 해상도 안에서 같은 크기로 또 바뀔 수 있어 막 바뀐 입력은 기억하지 않음
    if result[1] == "" and time.time_ns() - newest > INSPECTION_SETTLE_NS:
        with INSPECTIONS_LOCK:
            if len(INSPECTIONS) >= INSPECTION_LIMIT:
                INSPECTIONS.clear()
            INSPECTIONS[document] = (key, copy.deepcopy(result))
    return result


def transcript_of(location, mission, sessions):
    session = location.active.session if mission.active else ""
    recorded = [row["transcript"] for row in sessions if not session or row["session"] == session]
    found = next((path for path in reversed(recorded) if path and os.path.isfile(path)), "")
    if found or not mission_state.SESSION_ID.fullmatch(session):
        return found
    found = fallback_transcript(session, os.path.expanduser("~"), int(time.monotonic() // 30))
    return found if found and os.path.isfile(found) else ""


@functools.lru_cache(maxsize=128)
def fallback_transcript(session, home, _window):
    # Only sessions without a usable recorded path need the account-wide lookup.
    return (
        mission_state.search_claude(session, home)
        or mission_state.search_codex(session, home)
        or grok_transcript.find_transcript(session, home)
    )


def read_turn(path):
    if grok_transcript.is_transcript(path):
        activity = grok_transcript.activity(path)
        return parse_time(activity["last_timestamp"]), activity["completed"], {}
    last, ended, handed_back = 0.0, False, False
    stopped = {}
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - TRANSCRIPT_TAIL_BYTES))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if kind in ACTIVITY_TYPES:
            last = max(last, parse_time(entry.get("timestamp")))
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        blocks = [block for block in content if isinstance(block, dict)]
        if kind == "assistant":
            handed_back = any(
                block.get("type") == "tool_use" and block.get("name") == HANDBACK_TOOL for block in blocks
            )
            ended = handed_back or message.get("stop_reason") == "end_turn"
        elif kind == "user":
            # 하위 에이전트는 보고 도구의 결과 줄로 끝남
            ended = (
                ended
                and handed_back
                and bool(blocks)
                and all(block.get("type") == "tool_result" for block in blocks)
            )
            for task in TASK_NOTIFICATION.findall(line):
                stopped[task] = parse_time(entry.get("timestamp"))
        elif kind == "event_msg" and payload.get("type") in CODEX_TURN_EVENTS:
            ended = payload.get("type") == CODEX_TURN_DONE
    return last, ended, stopped


def recent_subagents(transcript):
    claude = os.path.join(os.path.splitext(transcript)[0], "subagents")
    if os.path.isdir(claude):
        found = [(SUBAGENT_FILE.fullmatch(name), os.path.join(claude, name)) for name in os.listdir(claude)]
        return [(agent.group(1), path) for agent, path in found if agent and fresh(path)]
    parent = CODEX_ROLLOUT.fullmatch(os.path.basename(transcript))
    if not parent:
        return []
    # Codex 하위 에이전트는 날짜 폴더(sessions/YYYY/MM/DD)에 따로 rollout 을 만들고 첫 줄에 부모 id 를 적음
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(transcript))))
    days = {
        time.strftime("%Y/%m/%d", time.localtime(time.time() - back)) for back in (0, SUBAGENT_STALE_SECONDS)
    }
    children = []
    folders = CODEX_CHILDREN.get()
    if folders is None:
        folders = {}
    for folder in (os.path.join(root, day) for day in days):
        if folder not in folders:
            by_parent = {}
            names = os.listdir(folder) if os.path.isdir(folder) else []
            for name in names:
                child = CODEX_ROLLOUT.fullmatch(name)
                if not child:
                    continue
                path = os.path.join(folder, name)
                try:
                    parent_id = codex_parent_of(path) if fresh(path) else ""
                except OSError:
                    continue
                if parent_id and parent_id != child.group(1):
                    by_parent.setdefault(parent_id, []).append((child.group(1), path))
            folders[folder] = by_parent
        children.extend(folders[folder].get(parent.group(1), ()))
    return children


def codex_parent_of(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline()
    try:
        meta = json.loads(first).get("payload") or {}
    except (ValueError, AttributeError):
        return ""
    return str(meta.get("parent_thread_id") or "") if isinstance(meta, dict) else ""


def grok_children(path):
    # Normal transcript appends do not change the child lifecycle. The saved
    # tail also detects truncation, rotation, and rewritten append boundaries.
    metadata = []
    for item in sorted(Path(path).parent.glob("subagents/*/meta.json")):
        try:
            info = item.stat()
        except OSError:
            continue
        metadata.append((item, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    key = (os.path.abspath(path), grok_transcript.grok_home())
    with open(path, "rb") as stream:
        info = os.fstat(stream.fileno())
        identity = (info.st_dev, info.st_ino)
        size = info.st_size
        written = (info.st_mtime_ns, info.st_ctime_ns)
        stream.seek(max(0, size - 128))
        tail = stream.read(min(size, 128))
        previous = GROK_CHILDREN.get(key)
        children = None
        if previous:
            old_identity, old_size, old_written, old_tail, old_metadata, old_children = previous
            if identity == old_identity and metadata == old_metadata:
                if (size, written) == (old_size, old_written):
                    children = old_children
                elif size > old_size and old_tail.endswith(b"\n"):
                    stream.seek(old_size - len(old_tail))
                    if stream.read(len(old_tail)) == old_tail and not any(
                        marker in line
                        for line in iter(stream.readline, b"")
                        for marker in GROK_LIFECYCLE_MARKERS
                    ):
                        children = old_children
    if children is None:
        children = grok_transcript.subagent_records(path)
    with GROK_CHILDREN_LOCK:
        if len(GROK_CHILDREN) >= 128:
            GROK_CHILDREN.clear()
        GROK_CHILDREN[key] = (identity, size, written, tail, metadata, children)
    return children


def read_session(mission, transcript):
    if not transcript:
        return
    mission.last_activity, mission.waiting_input, stopped = read_turn(transcript)
    if grok_transcript.is_transcript(transcript):
        children = grok_children(transcript)
        mission.background = any(child["status"] == "running" for child in children)
        for child in children:
            last = max(parse_time(child["started_at"]), parse_time(child["completed_at"]))
            path = child["transcript"]
            if child["status"] == "running" and (not path or not os.path.isfile(path)):
                # Child recording can start after the parent lifecycle event.
                path = grok_transcript.find_transcript(child["child_session_id"])
            if path and os.path.isfile(path):
                activity = grok_transcript.activity(path)
                last = max(last, parse_time(activity["last_timestamp"]))
            mission.last_activity = max(mission.last_activity, last)
        return
    for agent, path in recent_subagents(transcript):
        last, ended, _ = read_turn(path)
        notified = agent in stopped and stopped[agent] >= last
        mission.background = mission.background or not (ended or notified)
        mission.last_activity = max(mission.last_activity, last)


def mission_folder_allowed(location, mission_id):
    return dashboard_discovery.mission_folder_allowed(location.state_dir, mission_id)


def load_mission(location, mission_id, *, overview=False):
    folder = os.path.join(location.state_dir, mission_id)
    active = not location.active.corrupt and location.active.id == mission_id
    mission = Mission(location.label, mission_id, folder, active)
    mission.location_key = location.key or location.label
    mission.stale = location.stale if active else ""
    errors = mission.errors
    if not mission_folder_allowed(location, mission_id):
        errors.append("미션 폴더 경로")
        return mission
    if not dashboard_discovery.mission_metadata_allowed(location.state_dir, mission_id):
        errors.append("mission.md 경로")
        return mission
    lines = attempt(
        errors,
        "mission.md",
        os.path.join(folder, "mission.md"),
        lambda p: mission_state.read_text(p).split("\n"),
        [],
    )
    mission.fields = mission_state.header_fields(lines)
    mission.title = next((line[2:].strip() for line in lines if line.startswith("# ")), mission_id)
    mission.goal = section_text(lines, "목표")
    historical_summary = overview and mission.state in ("중단", "닫힘") and not mission.active
    automatic = attempt(errors, "질문 문서", folder, mission_state.auto_question_documents, set())
    try:
        automatic |= set(question_records.document_names(folder))
    except (OSError, ValueError) as error:
        mission.question_issues["questions/"] = str(error)
    report = []
    if not historical_summary:
        shared = attempt(
            errors, mission_state.SHARED_DOCUMENTS_FILE, folder, mission_state.shared_document_rows, []
        )
        if any(row["path"] == "report.md" for row in shared):
            report = attempt(
                errors,
                "공유 문서",
                os.path.join(folder, "report.md"),
                lambda p: read_shared_text(folder, p).split("\n"),
                [],
            )
    mission.now = section_text(report, "지금")
    if mission.now:
        mission.report_updated = modified_at(os.path.join(folder, "report.md"))
    mission.units = attempt(
        errors,
        mission_state.UNITS_FILE,
        os.path.join(folder, mission_state.UNITS_FILE),
        lambda p: mission_state.read_table(p, mission_state.UNIT_HEADER),
        [],
    )
    if not historical_summary and (not overview or (mission.active and mission.state == RUNNING)):
        sessions = attempt(
            errors,
            mission_guard.SESSIONS_FILE,
            os.path.join(folder, mission_guard.SESSIONS_FILE),
            lambda p: mission_state.read_table(p, mission_guard.SESSIONS_HEADER),
            [],
        )
        try:
            read_session(mission, transcript_of(location, mission, sessions))
        except OSError:
            errors.append("대화 기록")
    legacy_archive = (
        not mission.active
        and mission.state in ("중단", "닫힘")
        and not os.path.lexists(os.path.join(folder, "questions"))
    )
    for name in (() if historical_summary else sorted(automatic, key=natural_key)):
        if "/" in name:
            continue
        try:
            text = read_shared_text(folder, os.path.join(folder, name))
        except READ_ERRORS as error:
            mission.question_issues[name] = f"Markdown 읽기 실패: {error}"
            continue
        if (
            legacy_archive
            and not QUESTION_RECORD_HEADER.search(text)
            and not os.path.lexists(os.path.join(folder, "details", "history", name))
        ):
            mission.archived_questions.append(name)
            continue
        summary, issue = inspect_question(folder, name, text)
        if issue:
            if not os.path.lexists(os.path.join(folder, "questions", f"{name[:-3]}.json")):
                issue = f"질문 JSON 정본 누락 · 정형 전환 필요: {issue}"
            mission.question_issues[name] = issue
            continue
        mission.questions[name] = summary
        if name.startswith("confirm-") and SKELETON_HEADING.search(text):
            mission.skeletons.append(name)
    mission.unanswered = [name for name, summary in mission.questions.items() if summary.pending]
    sources = ["mission.md", mission_state.UNITS_FILE]
    sources.extend(automatic)
    sources.extend(f"questions/{name[:-3]}.json" for name in automatic if "/" not in name)
    mission.state_updated = max(
        (modified_at(os.path.join(folder, name)) for name in sources if name not in errors), default=0.0
    )
    return mission


def read_location(item):
    location = Location(
        item.label,
        item.state_dir,
        item.row,
        mission_guard.read_active(item.state_dir),
        item.root,
        key=item.key,
        stages=item.stages,
        stale_hours=item.stale_hours,
    )
    location.stale = stale_of(location)
    return location


def locations(config):
    catalog = workspace_access.catalog(config)
    found = [
        read_location(item) for item in catalog.workspaces
        if not item.requires_mission or dashboard_discovery.has_mission(item.state_dir)
    ]
    if found:
        found[0].problem = "; ".join(catalog.problems)
    return found


def mission_location(config, slot, mission_id):
    item = workspace_access.catalog(config).by_key.get(slot)
    if item is None or not dashboard_discovery.mission_metadata_allowed(item.state_dir, mission_id):
        return None
    return read_location(item)


def stale_of(location):
    active = location.active
    limit = location.stale_hours
    if not active.id or active.corrupt or not limit:
        return ""
    recorded = max(
        mission_guard.iso_epoch(active.opened_at),
        modified_at(mission_guard.active_path_of(location.state_dir)),
    )
    if time.time() - recorded < limit * 3600:
        return ""
    try:
        state = mission_guard.staleness(location.state_dir, active, limit)
    except Exception:
        return ""
    return mission_guard.stale_text(state) if state.stale else ""


def collect(config, *, overview=False):
    catalog = workspace_access.catalog(config)
    found, problems = [], list(catalog.problems)
    for item in catalog.workspaces:
        try:
            ids = dashboard_discovery.mission_ids(item.state_dir)
        except FileNotFoundError:
            if not item.requires_mission:
                found.append(read_location(item))
            continue
        except OSError:
            if item.requires_mission:
                problems.append(f"미션 폴더 읽기 실패: {item.state_dir}")
            else:
                location = read_location(item)
                location.problem = "미션 폴더 읽기 실패"
                found.append(location)
            continue
        if item.requires_mission and not ids:
            continue
        location = read_location(item)
        location.missions = [
            load_mission(location, mission_id, overview=overview) for mission_id in sorted(ids, reverse=True)
        ]
        found.append(location)
    if found:
        found[0].problem = "; ".join(filter(None, [*problems, found[0].problem]))
    return found
