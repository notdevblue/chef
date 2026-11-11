import json
import os
import re
import time
from datetime import datetime, timezone

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
MESSAGE_ROLES = {"user_message_chunk": "user", "agent_message_chunk": "assistant"}
ACTIVE_UPDATES = {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"}
SUBAGENT_STATUSES = {"running", "completed", "failed", "cancelled"}
ACTIVITY_TAIL_BYTES = 256 * 1024


def inside(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def grok_home(home=""):
    return os.environ.get("GROK_HOME") or os.path.join(home or os.path.expanduser("~"), ".grok")


def find_transcript(session, home="", require_unique=False):
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return ""
    root = os.path.join(grok_home(home), "sessions")
    if not os.path.isdir(root):
        return ""
    found = ""
    for name in sorted(os.listdir(root)):
        candidate = os.path.join(root, name, session, "updates.jsonl")
        if os.path.isfile(candidate) and inside(candidate, root):
            if require_unique:
                if found and os.path.realpath(found) != os.path.realpath(candidate):
                    raise ValueError("ambiguous Grok session transcript fallback")
                found = candidate
            else:
                return candidate
    return found


def entries(path, tail_bytes=0):
    with open(path, "rb") as handle:
        if tail_bytes:
            handle.seek(0, os.SEEK_END)
            offset = max(0, handle.tell() - tail_bytes)
            if offset:
                handle.seek(offset - 1)
                if handle.read(1) != b"\n":
                    handle.readline()
            else:
                handle.seek(0)
        for line in handle:
            try:
                entry = json.loads(line.decode("utf-8-sig", errors="replace"))
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def is_transcript(path):
    for entry in entries(path):
        params = entry.get("params")
        if isinstance(params, dict) and isinstance(params.get("update"), dict):
            if isinstance(params["update"].get("sessionUpdate"), str):
                return True
    return False


def session_id(path):
    for entry in entries(path):
        params = entry.get("params")
        if isinstance(params, dict):
            session = params.get("sessionId")
            if isinstance(session, str) and session:
                return session
    if os.path.basename(path) == "updates.jsonl":
        return os.path.basename(os.path.dirname(os.path.abspath(path)))
    return ""


def timestamp_of(entry, params):
    meta = params.get("_meta")
    value = meta.get("agentTimestampMs") if isinstance(meta, dict) else ""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value /= 1000
        else:
            value = entry.get("timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            return ""
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return ""


def iter_events(path, tail_bytes=0):
    for entry in entries(path, tail_bytes):
        params = entry.get("params")
        if not isinstance(params, dict):
            continue
        update = params.get("update")
        if not isinstance(update, dict):
            continue
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str):
            continue
        role = MESSAGE_ROLES.get(kind, "activity")
        if kind == "turn_completed":
            role = "completed"
        content = update.get("content")
        text = ""
        if role in ("user", "assistant") and isinstance(content, dict) and content.get("type") == "text":
            if isinstance(content.get("text"), str):
                text = content["text"]
        session = params.get("sessionId")
        if not isinstance(session, str):
            session = ""
        yield {"role": role, "text": text, "timestamp": timestamp_of(entry, params), "session_id": session, "update": kind}


def read_events(path):
    return list(iter_events(path))


def read_messages(path):
    messages = []
    pending = {}
    for event in iter_events(path):
        if event["role"] == "completed":
            if pending:
                messages.append(pending)
                pending = {}
        elif event["role"] in ("user", "assistant"):
            if pending and (pending["role"] != event["role"] or pending["session_id"] != event["session_id"]):
                messages.append(pending)
                pending = {}
            if not event["text"]:
                continue
            if pending:
                pending["text"] += event["text"]
                pending["timestamp"] = event["timestamp"] or pending["timestamp"]
            else:
                pending = dict(event)
    if pending:
        messages.append(pending)
    return messages


def activity_state(events):
    result = {"session_id": "", "last_timestamp": "", "last_role": "", "completed": False}
    for event in events:
        if event["session_id"]:
            result["session_id"] = event["session_id"]
        if event["role"] == "completed":
            result["last_timestamp"] = event["timestamp"] or result["last_timestamp"]
            result["last_role"] = "completed"
            result["completed"] = True
        elif event["update"] in ACTIVE_UPDATES:
            result["last_timestamp"] = event["timestamp"] or result["last_timestamp"]
            result["last_role"] = event["role"]
            result["completed"] = False
    return result


def activity(path):
    result = activity_state(iter_events(path, ACTIVITY_TAIL_BYTES))
    if not result["last_role"]:
        result = activity_state(iter_events(path))
    if not result["session_id"] and os.path.basename(path) == "updates.jsonl":
        result["session_id"] = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return result


def subagent_record(value, parent, home):
    if not isinstance(value, dict) or value.get("parent_session_id") != parent:
        return {}
    for key in ("subagent_id", "child_session_id"):
        if not isinstance(value.get(key), str) or not SESSION_ID.fullmatch(value[key]):
            return {}
    attempt = value.get("attempt_id", "")
    if attempt is None:
        attempt = ""
    if not isinstance(attempt, str) or (attempt and not SESSION_ID.fullmatch(attempt)):
        return {}
    status = value.get("status")
    if not isinstance(status, str) or status not in SUBAGENT_STATUSES or value["child_session_id"] == parent:
        return {}
    return {
        "subagent_id": value["subagent_id"], "parent_session_id": parent,
        "child_session_id": value["child_session_id"], "attempt_id": attempt, "status": value["status"],
        "started_at": timestamp_of({"timestamp": value.get("started_at")}, {}),
        "completed_at": timestamp_of({"timestamp": value.get("completed_at")}, {}),
        "transcript": find_transcript(value["child_session_id"], home),
    }


def subagent_records(path, home=""):
    parent = session_id(path)
    if not parent:
        return []
    records = {}
    for entry in entries(path):
        params = entry.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != parent:
            continue
        update = params.get("update")
        if not isinstance(update, dict) or update.get("sessionUpdate") not in ("subagent_spawned", "subagent_finished"):
            continue
        if update.get("parent_session_id", parent) != parent:
            continue
        value = {**update, "parent_session_id": parent}
        stamp = timestamp_of(entry, params)
        if update["sessionUpdate"] == "subagent_spawned":
            value.update(status="running", started_at=stamp, completed_at="")
        else:
            value["completed_at"] = stamp
        record = subagent_record(value, parent, home)
        if not record:
            continue
        previous = records.get(record["subagent_id"], {})
        if update["sessionUpdate"] == "subagent_finished" and previous:
            if any(previous[key] != record[key] for key in ("attempt_id", "child_session_id")):
                continue
            record["started_at"] = previous["started_at"]
        records[record["subagent_id"]] = record
    folder = os.path.dirname(os.path.abspath(path))
    root = os.path.join(folder, "subagents")
    if os.path.isdir(root) and inside(root, folder):
        for name in sorted(os.listdir(root)):
            if not SESSION_ID.fullmatch(name):
                continue
            meta_path = os.path.join(root, name, "meta.json")
            if not os.path.isfile(meta_path) or not inside(meta_path, root):
                continue
            try:
                with open(meta_path, encoding="utf-8-sig") as handle:
                    value = json.load(handle)
            except (OSError, ValueError):
                continue
            record = subagent_record(value, parent, home)
            if record and record["subagent_id"] == name:
                records[name] = merge_subagent_record(records.get(name, {}), record)
    return [records[key] for key in sorted(records)]


def subagent_transcripts(path, home=""):
    return sorted({record["transcript"] for record in subagent_records(path, home) if record["transcript"]})


def merge_subagent_record(lifecycle, metadata):
    if not lifecycle:
        return metadata
    if any(lifecycle[key] != metadata[key] for key in ("attempt_id", "child_session_id")):
        if lifecycle["started_at"] and metadata["started_at"] and metadata["started_at"] > lifecycle["started_at"]:
            return metadata
        return lifecycle
    if lifecycle["status"] == "running":
        return metadata
    if metadata["status"] == "running":
        return lifecycle
    if lifecycle["completed_at"] and metadata["completed_at"] and metadata["completed_at"] > lifecycle["completed_at"]:
        return metadata
    return lifecycle


TERMINAL_TOOL_STATUSES = {"completed", "failed", "cancelled", "canceled", "error"}
WAKE_GRACE_SECONDS = 180


def entry_seconds(entry, params):
    meta = params.get("_meta")
    value = meta.get("agentTimestampMs") if isinstance(meta, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000
    value = entry.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def turn_events_busy(path):
    # events.jsonl 은 turn_started·turn_ended 를 따로 남김. 끝 없는 시작이 마지막이면 턴이 도는 중
    events = os.path.join(os.path.dirname(os.path.abspath(path)), "events.jsonl")
    if not os.path.isfile(events):
        return False
    state = False
    with open(events, "rb") as handle:
        for line in handle:
            try:
                entry = json.loads(line.decode("utf-8-sig", errors="replace"))
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "turn_started":
                state = True
            elif isinstance(entry, dict) and entry.get("type") == "turn_ended":
                state = False
    return state


def first_line(value, limit=60):
    text = str(value or "").strip().splitlines()
    return text[0][:limit] if text else ""


def busy_reasons(path, home="", now=None):
    """세션이 쉬는 중이 아니면 이유 목록, 쉬는 중이면 빈 목록.

    쉬는 중 = 마지막 턴이 turn_completed 로 끝났고, 그 턴에 끝나지 않은 도구 호출이 없고,
    실행 중인 서브에이전트·백그라운드 작업이 없고, 서브에이전트 깨우기(will_wake)로 곧 시작될 턴도 없음.
    """
    parent = session_id(path)
    now = time.time() if now is None else now
    in_turn = False
    seen_turn = False
    tools = {}
    background = {}
    subagents = {}
    wakes = []
    for entry in entries(path):
        params = entry.get("params")
        if not isinstance(params, dict):
            continue
        if parent and params.get("sessionId") not in (None, parent):
            continue
        update = params.get("update")
        if not isinstance(update, dict) or not isinstance(update.get("sessionUpdate"), str):
            continue
        kind = update["sessionUpdate"]
        if kind in ACTIVE_UPDATES:
            if not in_turn:
                tools = {}
                wakes = []
            in_turn = seen_turn = True
        call = update.get("toolCallId")
        if kind == "tool_call" and isinstance(call, str):
            tools[call] = (update.get("status") or "pending", update.get("title") or "")
        elif kind == "tool_call_update" and isinstance(call, str):
            status, title = tools.get(call, ("pending", ""))
            tools[call] = (update.get("status") or status, update.get("title") or title)
        elif kind == "turn_completed":
            in_turn = False
            seen_turn = True
            tools = {}
        elif kind == "background_tasks" and isinstance(update.get("tasks"), list):
            background = {
                str(task.get("task_id")): task.get("description") or task.get("command")
                for task in update["tasks"]
                if isinstance(task, dict) and task.get("status", "running") == "running"
            }
        elif kind == "task_backgrounded" and (update.get("task_id") or update.get("tool_call_id")):
            background[str(update.get("task_id") or update.get("tool_call_id"))] = update.get("description") or update.get("command")
        elif kind == "task_completed":
            snapshot = update.get("task_snapshot")
            task = snapshot.get("task_id") if isinstance(snapshot, dict) else update.get("task_id")
            background.pop(str(task), None)
        elif kind == "subagent_spawned" and update.get("subagent_id"):
            subagents[str(update["subagent_id"])] = update.get("description") or ""
        elif kind == "subagent_finished":
            subagents.pop(str(update.get("subagent_id")), None)
            if update.get("will_wake") and not in_turn:
                wakes.append((entry_seconds(entry, params), str(update.get("subagent_id") or "")))
    # subagents/<id>/meta.json 의 status 가 더 늦게 갱신될 수 있어 같이 봄 (attempt_id 형식은 따지지 않음)
    root = os.path.join(os.path.dirname(os.path.abspath(path)), "subagents")
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            try:
                with open(os.path.join(root, name, "meta.json"), encoding="utf-8-sig") as handle:
                    meta = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict) or (parent and meta.get("parent_session_id") not in (None, parent)):
                continue
            if meta.get("status") == "running":
                subagents.setdefault(name, meta.get("description") or "")
            elif meta.get("status") in SUBAGENT_STATUSES:
                subagents.pop(name, None)
    reasons = []
    if in_turn:
        reasons.append("마지막 턴이 아직 안 끝남 (turn_completed 없음)")
    elif not seen_turn:
        reasons.append("끝난 턴 기록이 없음")
    elif turn_events_busy(path):
        reasons.append("events.jsonl 에 끝나지 않은 turn_started 가 있음")
    pending = [f"{call} {first_line(title)}".strip() for call, (status, title) in tools.items() if status not in TERMINAL_TOOL_STATUSES]
    if pending:
        reasons.append(f"끝나지 않은 도구 호출 {len(pending)}개: " + ", ".join(pending[:5]))
    if subagents:
        texts = [f"{subagent} {first_line(text)}".strip() for subagent, text in subagents.items()]
        reasons.append(f"실행 중인 서브에이전트 {len(texts)}개: " + ", ".join(texts[:5]))
    if background:
        texts = [f"{task} {first_line(text)}".strip() for task, text in background.items()]
        reasons.append(f"실행 중인 백그라운드 작업 {len(texts)}개: " + ", ".join(texts[:5]))
    waking = [subagent for stamp, subagent in wakes if now - stamp < WAKE_GRACE_SECONDS]
    if waking:
        reasons.append("서브에이전트가 끝나며 메인을 깨우는 중 (will_wake): " + ", ".join(waking))
    return reasons
