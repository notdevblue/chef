"""dashboard / data."""

import copy
import functools
import json
import os
import threading
import time

from chef.dashboard import discovery as dashboard_discovery
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


def request_scoped(function):
    @functools.wraps(function)
    def scoped(*args, **kwargs):
        with infrastructure_paths.request_realpaths():
            return function(*args, **kwargs)

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
    home = os.path.expanduser("~")
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
    for folder in (os.path.join(root, day) for day in days):
        names = os.listdir(folder) if os.path.isdir(folder) else []
        for child, path in ((CODEX_ROLLOUT.fullmatch(name), os.path.join(folder, name)) for name in names):
            if (
                child
                and child.group(1) != parent.group(1)
                and fresh(path)
                and codex_parent_of(path) == parent.group(1)
            ):
                children.append((child.group(1), path))
    return children


def codex_parent_of(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline()
    try:
        meta = json.loads(first).get("payload") or {}
    except (ValueError, AttributeError):
        return ""
    return str(meta.get("parent_thread_id") or "") if isinstance(meta, dict) else ""


def read_session(mission, transcript):
    if not transcript:
        return
    mission.last_activity, mission.waiting_input, stopped = read_turn(transcript)
    if grok_transcript.is_transcript(transcript):
        children = grok_transcript.subagent_records(transcript)
        mission.background = any(child["status"] == "running" for child in children)
        for child in children:
            last = max(parse_time(child["started_at"]), parse_time(child["completed_at"]))
            path = child["transcript"]
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


def load_mission(location, mission_id):
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
    shared = attempt(
        errors, mission_state.SHARED_DOCUMENTS_FILE, folder, mission_state.shared_documents, set()
    )
    automatic = attempt(errors, "질문 문서", folder, mission_state.auto_question_documents, set())
    try:
        automatic |= set(question_records.document_names(folder))
    except (OSError, ValueError) as error:
        mission.question_issues["questions/"] = str(error)
    report = []
    if "report.md" in shared:
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
    for name in sorted(automatic, key=natural_key):
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


def locations(config):
    workspaces, problems = dashboard_discovery.discover(config)
    found = [
        Location(
            item.label,
            item.state_dir,
            item.row,
            mission_guard.read_active(item.state_dir),
            item.root,
            key=item.key,
            stages=item.stages,
            stale_hours=item.stale_hours,
        )
        for item in workspaces
    ]
    if found:
        found[0].problem = "; ".join(problems)
    for location in found:
        location.stale = stale_of(location)
    return found


def stale_of(location):
    active = location.active
    if not active.id or active.corrupt:
        return ""
    try:
        state = mission_guard.staleness(location.state_dir, active, location.stale_hours)
    except Exception:
        return ""
    return mission_guard.stale_text(state) if state.stale else ""


def collect(config):
    found = locations(config)
    for location in found:
        try:
            ids = dashboard_discovery.mission_ids(location.state_dir)
        except FileNotFoundError:
            continue
        except OSError:
            location.problem = "미션 폴더 읽기 실패"
            continue
        location.missions = [load_mission(location, mission_id) for mission_id in sorted(ids, reverse=True)]
    return found
