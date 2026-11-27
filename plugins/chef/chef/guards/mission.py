"""guards / mission."""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from chef.guards import questions as question_write_guard
from chef.guards.allow_once import SharedAllowOnceError, configured_participants, consume_shared_allow_once, shared_once_context, normalize_shared_payload
from chef.guards.policy import mission_policy_configured, mission_policy_denial
from chef.guards.adapters import (
    AdapterNotFound,
    GuardOff,
    Located,
    Registry,
    adapter_relatives,
    ancestors,
    codex_config_registry_dir,
    find_adapter,
    han_repo_adapters_dir,
    in_place_adapters_dir,
    load_adapter,
    load_registry,
    locate_adapter,
    merge_local,
    normalize_path,
    read_json_file,
    read_local,
    registry_adapter_path,
    registry_dir,
    registry_missing_message,
    resolve_registry,
    root_matches,
    runtime_overlay,
    state_dir_of,
    valid_mission_id,
)
from chef.guards.contracts import (
    ADAPTER_RELATIVE,
    BACKTICK_BODY,
    BASH_ASSIGN,
    BASH_GROUP_OPENER,
    CELL_BREAKS,
    CHAIN_PREFIX,
    COMMAND_SUBSTITUTION,
    CONTINUATIONS,
    CORRUPT_ACTIVE,
    ESCAPE_CHARS,
    FIRST_TOKEN_REST,
    FORCE_CLOSE_HINT,
    GUARDED_TOOLS,
    HEARTBEAT_SECONDS,
    LITERAL_ARGUMENT,
    LITERAL_COMMAND,
    LITERAL_SEARCH_OPTIONS,
    LOCAL_FILE,
    LOG_COMMAND_LENGTH,
    LOG_ENV,
    LOG_FIELDS,
    MARKER,
    MARKER_ALONE_REASON,
    MAX_DEPTH,
    MISSION_ID,
    NO_ACTIVE,
    NOT_STALE,
    P4_EXECUTABLES,
    P4_READ_FLAG_COMMANDS,
    P4_TOKEN,
    P4_VALUE_OPTIONS,
    P4_WRITE_COMMANDS,
    P4C_CALL,
    P4C_NAME,
    P4C_OWN_FLAGS,
    P4C_OWN_VALUE_OPTIONS,
    P4C_SCRIPT,
    POWERSHELL_ASSIGN,
    POWERSHELL_GROUP_OPENER,
    POWERSHELL_SUBEXPRESSION_OPENER,
    QUOTED_TEXT,
    REGISTRY_ENV,
    REGISTRY_FILE,
    REPO_ENV,
    SESSION_ACTIONS,
    SESSIONS_FILE,
    SESSIONS_HEADER,
    SIMPLE_VALUE,
    STALE_ENV,
    STALE_HOURS_DEFAULT,
    STALE_SCAN_LIMIT,
    SUPPORTED_SCHEMA,
    USER_CONFIG_ENV,
    VARIABLE_REFERENCE,
    WRAPPER_BODIES,
    WRAPPERS,
    WRITE_TOOLS,
    Active,
    Outcome,
    Staleness,
    Verdict,
)
from chef.guards.logging import append_log, log_payload, now_iso
from chef.guards.p4 import (
    expected_p4_client,
    p4_client_reason,
    p4_deny_reason,
    p4_invocation,
    p4_policy,
    p4c_as_p4,
    p4c_project_reason,
)
from chef.guards.shell import (
    Variables,
    allowed_fragments,
    command_fragments,
    compile_patterns,
    expand,
    group_bodies,
    is_redirect_ampersand,
    join_continuations,
    literal_data_command,
    peel_assignment,
    peel_wrapper,
    pattern_flags,
    rule_exceptions,
    split_fragments,
    strip_chain_prefix,
    substitute,
    substitution_bodies,
    unquote,
)
from chef.runtimes.hooks import is_grok_subagent, normalize_payload
from chef.verification import execution


def active_path_of(state_dir):
    return os.path.join(state_dir, "ACTIVE")


def read_active(state_dir):
    try:
        with open(active_path_of(state_dir), encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return NO_ACTIVE
    except OSError:
        return CORRUPT_ACTIVE
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return CORRUPT_ACTIVE
        if not isinstance(data, dict):
            return CORRUPT_ACTIVE
        return Active(
            str(data.get("id") or "-"),
            str(data.get("session") or ""),
            str(data.get("openedAt") or ""),
            runtime=str(data.get("runtime") or ""),
        )
    return Active(text.splitlines()[0].strip() if text.strip() else "-", "", "")


def write_active(state_dir, active):
    os.makedirs(state_dir, exist_ok=True)
    final = active_path_of(state_dir)
    temp = f"{final}.{os.getpid()}.tmp"
    data = {"id": active.id, "session": active.session, "openedAt": active.opened_at}
    if active.runtime:
        data["runtime"] = active.runtime
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        os.replace(temp, final)
    except BaseException:
        remove_quietly(temp)
        raise


def remove_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def stale_hours_of(adapter):
    # 우선순위: HAN_STALE_HOURS 환경변수 > adapter(local.json 병합) mission.staleHours > 24. 0 이면 끊김 판정 끔
    raw = os.environ.get(STALE_ENV, "")
    if raw.strip() == "":
        raw = ((adapter or {}).get("mission") or {}).get("staleHours", STALE_HOURS_DEFAULT)
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return float(STALE_HOURS_DEFAULT)
    return hours if hours >= 0 else float(STALE_HOURS_DEFAULT)


def iso_epoch(text):
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


def newest_mtime(folder, limit=STALE_SCAN_LIMIT):
    newest, seen = 0.0, 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in files:
            seen += 1
            if seen > limit:
                return newest
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                pass
    return newest


def last_activity(state_dir, active):
    # 활동 = openedAt, ACTIVE 파일 mtime(같은 세션 명령마다 heartbeat 가 갱신), 미션 폴더 안 파일 중 가장 최근 mtime
    candidates = [iso_epoch(active.opened_at)]
    try:
        candidates.append(os.path.getmtime(active_path_of(state_dir)))
    except OSError:
        pass
    if active.id and valid_mission_id(active.id):
        candidates.append(newest_mtime(os.path.join(state_dir, active.id)))
    return max(candidates)


def staleness(state_dir, active, limit, now=None):
    if not active.id or active.corrupt:
        return NOT_STALE
    last = last_activity(state_dir, active)
    now = time.time() if now is None else now
    hours = max(0.0, (now - last) / 3600) if last else float("inf")
    return Staleness(bool(limit) and hours >= limit, last, hours, float(limit))


def age_text(hours):
    if hours == float("inf"):
        return "알 수 없음"
    if hours < 1:
        return f"{int(hours * 60)}분"
    if hours < 48:
        return f"{int(hours)}시간"
    return f"{hours / 24:.1f}일"


def stale_text(state):
    last = datetime.fromtimestamp(state.last).strftime("%Y-%m-%d %H:%M") if state.last else "기록 없음"
    return f"끊김 의심: 마지막 활동 {age_text(state.hours)} 전({last}), 기준 {state.limit:g}시간"


def heartbeat(state_dir, payload, now=None):
    # ACTIVE 를 다시 쓰지 않고 mtime 만 갱신. 그 사이 close 로 지워졌으면 utime 이 실패해 되살리지 않음
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    active = read_active(state_dir)
    if not active.id or active.corrupt or active.session != session_id:
        return
    path = active_path_of(state_dir)
    now = time.time() if now is None else now
    try:
        if now - os.path.getmtime(path) >= HEARTBEAT_SECONDS:
            os.utime(path, (now, now))
    except OSError:
        pass


def find_denied_reason(adapter, fragments, problems=None, command="", check_p4=True):
    problems = [] if problems is None else problems
    fragments = allowed_fragments(adapter, fragments, problems)
    if not fragments:
        return ""
    for rule in (adapter.get("guard") or {}).get("deny") or []:
        pattern = rule.get("pattern") if isinstance(rule, dict) else None
        if not isinstance(pattern, str):
            continue
        flags = pattern_flags(rule, problems, "guard.deny")
        if flags is None:
            continue
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            problems.append(f"잘못된 정규식 건너뜀: {pattern} ({e})")
            continue
        exceptions = rule_exceptions(rule, problems)
        command_exceptions = rule_exceptions(rule, problems, "exceptCommand")
        if command_exceptions and literal_data_command(command):
            if any(exception.fullmatch(command) for exception in command_exceptions):
                continue
        if any(
            compiled.search(fragment) and not any(exception.fullmatch(fragment) for exception in exceptions)
            for fragment in fragments
        ):
            return str(rule.get("reason") or "차단된 명령")
    return p4_deny_reason(adapter, fragments, problems) if check_p4 else ""


def contains_marker(command, tool_name="Bash"):
    return any(MARKER.match(fragment) for fragment in command_fragments(command, tool_name))


def judge_command(adapter, command, tool_name="Bash", problems=None, project="", cwd=None):
    from chef.guards.argv import wrapper_child

    fragments = command_fragments(command, tool_name)
    markers = [match for match in map(MARKER.match, fragments) if match]
    if markers:
        if len(fragments) == 1:
            return Verdict("marker", marker=markers[0])
        return Verdict("mixed", MARKER_ALONE_REASON)
    for fragment in fragments:
        tokens = [unquote(token) for token in P4_TOKEN.findall(fragment)]
        if tokens:
            try:
                wrapper_child(os.path.basename(tokens[0]).lower().removesuffix(".exe"), tokens[1:])
            except ValueError as error:
                return Verdict("rule", str(error))
        scope_reason = p4c_project_reason(fragment, project, adapter, cwd)
        if scope_reason:
            return Verdict("rule", scope_reason)
    wrapped = [p4 for p4 in (p4c_as_p4(fragment, project, adapter) for fragment in fragments) if p4]
    fragments = list(dict.fromkeys(fragments + wrapped))
    reason = find_denied_reason(adapter, fragments, problems, command) or p4_client_reason(
        adapter, fragments, project
    )
    return Verdict("rule", reason) if reason else Verdict("allow")


def judge_argv(adapter, argv, problems=None, project="", depth=0, cwd=None):
    """Judge direct argv as data; recurse only into an explicit shell command."""
    from chef.guards.argv import describe, render

    problems = [] if problems is None else problems
    if depth > MAX_DEPTH:
        return Verdict("rule", "명령 wrapper 중첩이 너무 깊어 가드를 확인할 수 없음")
    try:
        invocation = describe(argv)
    except ValueError as error:
        return Verdict("rule", str(error))
    command = render(invocation.argv)
    marker = MARKER.fullmatch(command)
    if marker:
        return Verdict("marker", marker=marker)
    scope_reason = p4c_project_reason(invocation.argv, project, adapter, cwd)
    if scope_reason:
        return Verdict("rule", scope_reason)
    wrapped = p4c_as_p4(invocation.argv, project, adapter)
    vectors = [invocation.argv] + ([wrapped] if wrapped else [])
    fragments = [command] + ([render(wrapped)] if wrapped else [])
    reason = find_denied_reason(adapter, fragments, problems, command, check_p4=False)
    if not reason:
        retained = [vector for vector, text in zip(vectors, fragments) if allowed_fragments(adapter, [text], problems)]
        reason = p4_deny_reason(adapter, retained, problems) or p4_client_reason(adapter, vectors, project)
    if reason:
        return Verdict("rule", reason)
    if invocation.shell:
        return judge_command(adapter, invocation.script, invocation.shell, problems, project, cwd)
    if invocation.child:
        return judge_argv(adapter, invocation.child, problems, project, depth + 1, cwd)
    return Verdict("allow")


def corrupt_active_notice(state_dir):
    return f"ACTIVE 손상: {active_path_of(state_dir)}, 확인 후 지우거나 close"


def allow_once_path(state_dir, mission_id):
    return os.path.join(state_dir, mission_id, "allow-once")


def clear_allow_once(state_dir, mission_id):
    path = allow_once_path(state_dir, mission_id)
    remove_quietly(path)
    folder = os.path.dirname(path)
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.startswith("allow-once.") and name.endswith(".claim"):
                remove_quietly(os.path.join(folder, name))


def open_mission(state_dir, mission_id, session_id, active, outcome, runtime=""):
    if active.corrupt:
        outcome.deny = corrupt_active_notice(state_dir)
        return
    if active.id and active.id != mission_id:
        outcome.deny = f"다른 미션 {active.id} 진행 중"
        return
    write_active(state_dir, Active(mission_id, session_id, now_iso(), runtime=runtime or active.runtime))
    outcome.messages.append(f"미션 {mission_id} 가드 켜짐")


def resume_mission(state_dir, mission_id, session_id, active, outcome, runtime=""):
    if active.corrupt:
        outcome.deny = corrupt_active_notice(state_dir)
        return
    if active.id != mission_id:
        outcome.deny = (
            f"다른 미션 {active.id} 진행 중" if active.id else f"미션 {mission_id} 가 열려 있지 않음"
        )
        return
    write_active(
        state_dir,
        Active(mission_id, session_id, active.opened_at or now_iso(), runtime=runtime or active.runtime),
    )
    outcome.messages.append(f"미션 {mission_id} 가드 세션 갱신 (이전 session: {active.session or '없음'})")


def close_mission(state_dir, mission_id, session_id, active, outcome, runtime=""):
    if active.corrupt:
        remove_quietly(active_path_of(state_dir))
        clear_allow_once(state_dir, mission_id)
        outcome.messages.append(f"손상된 ACTIVE 를 지움 (close {mission_id})")
        return
    if not active.id:
        clear_allow_once(state_dir, mission_id)
        outcome.messages.append(f"미션 {mission_id} 는 이미 닫혀 있음")
        return
    if active.id != mission_id:
        outcome.deny = f"다른 미션 {active.id} 진행 중"
        return
    if active.session and active.session != session_id:
        outcome.deny = f"다른 세션의 미션 {active.id} — resume 후 close"
        return
    remove_quietly(active_path_of(state_dir))
    clear_allow_once(state_dir, mission_id)
    outcome.messages.append(f"미션 {mission_id} 가드 꺼짐")


MARKER_ACTIONS = {"open": open_mission, "resume": resume_mission, "close": close_mission}


def handle_marker(
    marker, state_dir, session_id, outcome, transcript="", stale_hours=STALE_HOURS_DEFAULT, runtime=""
):
    action, mission_id = marker.group(1).lower(), marker.group(2)
    if not valid_mission_id(mission_id):
        outcome.deny = f"미션 id 형식 오류: {mission_id}"
        return
    active = read_active(state_dir)
    MARKER_ACTIONS[action](state_dir, mission_id, session_id, active, outcome, runtime)
    if outcome.deny and active.id and not active.corrupt:
        try:
            state = staleness(state_dir, active, stale_hours)
        except Exception:  # 안내만 포기
            state = NOT_STALE
        if state.stale:
            outcome.deny += f" — {active.id} {stale_text(state)}. {FORCE_CLOSE_HINT}"
    if action in SESSION_ACTIONS and not outcome.deny:
        try:
            folder = os.path.join(state_dir, mission_id)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, SESSIONS_FILE)
            lines = (
                ["\t".join(SESSIONS_HEADER)] if not os.path.isfile(path) or os.path.getsize(path) == 0 else []
            )
            cells = [now_iso(), action, session_id, transcript]
            lines.append("\t".join(CELL_BREAKS.sub(" ", cell).strip() for cell in cells))
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:  # 가드는 이미 켜졌으니 기록 실패로 마커를 거부하지 않음
            outcome.messages.append(f"{SESSIONS_FILE} 기록 실패: {e}")


def consume_allow_once(state_dir, mission_id, command):
    path = allow_once_path(state_dir, mission_id)
    if not valid_mission_id(mission_id) or not os.path.isfile(path):
        return False
    claim = f"{path}.{os.getpid()}.claim"
    try:
        os.replace(path, claim)
    except FileNotFoundError:
        return False
    try:
        with open(claim, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        wanted = command.strip()
        for index, line in enumerate(lines):
            if line.strip() != wanted:
                continue
            remaining = [rest for i, rest in enumerate(lines) if i != index and rest.strip()]
            if remaining:
                with open(claim, "w", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(remaining) + "\n")
                os.replace(claim, path)
            else:
                os.remove(claim)
            return True
        os.replace(claim, path)
        return False
    except BaseException:
        if os.path.exists(claim) and not os.path.exists(path):
            try:
                os.replace(claim, path)
            except OSError:
                pass
        raise


def runs_in_other_session(active, payload):
    if is_grok_subagent(payload):
        return False
    session_id = payload.get("session_id")
    if not active.session or not session_id:
        return False
    return active.session != session_id


def participates_in_mission(active, payload):
    session = payload.get("session_id")
    return (
        not active.corrupt
        and valid_mission_id(active.id)
        and bool(active.session)
        and isinstance(session, str)
        and bool(session)
        and not runs_in_other_session(active, payload)
    )


def allow_once_guidance(state_dir, mission_id, command, tool_name):
    wanted = command.strip()
    if len(wanted.splitlines()) != 1 or len(command_fragments(wanted, tool_name)) != 1:
        return (
            "1회 허용은 한 줄의 단일 명령으로 처리한다. 이 호출을 그대로 등록하지 말고, "
            "승인 대상 명령을 단독 호출로 정리한 뒤 승인 범위와 가드 판정을 다시 확인한다."
        )
    path = os.path.abspath(allow_once_path(state_dir, mission_id))
    return (
        "이 명령은 사용자 승인 후 allow-once로 1회 실행할 수 있다. "
        "현재 요청에서 이 실행을 명시적으로 승인받았으면 재질문하지 않는다. "
        "계획·리뷰 승인을 실행 승인으로 확대하지 않는다.\n"
        "승인 후 코디네이터가 파일 편집 도구로 아래 경로에 정확한 명령을 한 줄로 기록하고, "
        "같은 명령을 단독 실행한다. 사용자에게 파일 작성이나 명령 실행을 맡기지 않는다.\n"
        f"허용 파일: {path}\n"
        "기존의 다른 허용 줄은 보존한다. 한 번 통과하면 실행 실패 여부와 무관하게 소모된다. "
        "재실행 승인은 기존 미션·어댑터 정책을 따른다.\n"
        f"명령:\n{wanted}"
    )


def judge_denied_command(
    payload, command, active, state_dir, reason, outcome, stale_hours=STALE_HOURS_DEFAULT, shared=False
):
    try:
        if runs_in_other_session(active, payload):
            note = ""
            try:
                state = staleness(state_dir, active, stale_hours)
                note = f" ({stale_text(state)})" if state.stale else ""
            except Exception:  # 안내만 포기
                pass
            outcome.messages.append(
                f"미션 {active.id}가 다른 세션에서 진행 중{note} — 이 세션은 막지 않음: {reason}"
            )
        elif not shared and consume_allow_once(state_dir, active.id, command):
            outcome.messages.append(f"1회 허용 사용: {command.strip()}")
        else:
            guidance = allow_once_guidance(state_dir, active.id, command, payload.get("tool_name", "Bash"))
            outcome.deny = f"{reason} (미션 진행 중: {active.id})\n\n{guidance}"
    except Exception as e:  # 규칙에 걸린 뒤의 오류는 허용이 아니라 거부
        path = allow_once_path(state_dir, active.id)
        outcome.deny = f"{reason} (미션 진행 중: {active.id}; 1회 허용 확인 실패라 허용 안 함: {path} — {type(e).__name__}: {e})"


def apply_rule(payload, command, verdict, state_dir, problems, outcome, stale_hours=STALE_HOURS_DEFAULT, shared=False):
    active = read_active(state_dir)
    if not active.id:
        return
    outcome.messages.extend(problems)
    if active.corrupt:
        outcome.deny = f"{verdict.reason} ({corrupt_active_notice(state_dir)})"
        return
    judge_denied_command(payload, command, active, state_dir, verdict.reason, outcome, stale_hours, shared)


def report_problems_while_active(state_dir, problems, outcome):
    try:
        active = read_active(state_dir)
    except Exception:  # 규칙에 안 걸린 명령이라 알림만 포기
        return
    if active.id:
        outcome.messages.extend(problems)


def glob_regex(pattern):
    """`**` 는 여러 단계, `*`·`?` 는 한 단계 안. 슬래시로 비교하고 대소문자는 무시."""
    out = []
    i = 0
    text = pattern.replace("\\", "/").strip("/")
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def coordinator_write_globs(adapter, problems):
    value = ((adapter or {}).get("guard") or {}).get("coordinatorWrite")
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        problems.append("guard.coordinatorWrite 형식 오류: 문자열 배열이어야 함. 적용하지 않음")
        return None
    return value


def written_path(payload):
    tool_input = payload.get("tool_input") or {}
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def coordinator_write_reason(project, cwd, target, globs):
    """프로젝트 안 경로가 globs 어디에도 안 맞으면 거부 이유. 프로젝트 밖(메모리·임시 파일)은 보지 않음."""
    base = os.path.abspath(project)
    path = os.path.abspath(os.path.join(cwd or base, target))
    try:
        relative = os.path.relpath(path, base)
    except ValueError:  # 다른 드라이브
        return ""
    relative = relative.replace(os.sep, "/")
    if relative == ".." or relative.startswith("../"):
        return ""
    if any(glob_regex(pattern).fullmatch(relative) for pattern in globs):
        return ""
    return (
        f"코디네이터는 미션 중 프로젝트 파일을 직접 쓰지 않음: {relative} "
        f"(허용: {', '.join(globs)}). roles.implement 로 파견할 것"
    )


def decide_write(payload):
    outcome = Outcome()
    if is_grok_subagent(payload) or (payload.get("han_runtime") != "grok" and payload.get("agent_id")):
        return outcome
    target = written_path(payload)
    if not target:
        return outcome
    located = locate_adapter(payload)
    if not located:
        return outcome
    problems = []
    adapter = load_adapter(located.adapter_path, problems, runtime=payload.get("han_runtime", "claude"))
    globs = coordinator_write_globs(adapter, problems)
    if globs is None:
        return outcome
    active = read_active(state_dir_of(located.project, adapter))
    if not active.id or active.corrupt or not active.session:
        return outcome
    if active.session != str(payload.get("session_id") or ""):
        return outcome
    outcome.messages.extend(problems)
    outcome.deny = coordinator_write_reason(located.project, payload.get("cwd"), target, globs)
    return outcome


def execution_write_reason(payload):
    """Check only mutation paths statically resolved by supported tool syntax.

    This hook is a pre-use check, not an OS lock or a reservation spanning the
    tool invocation. Dynamic/uninstrumented writers still need the runner's
    post-execution hash checks.
    """
    if payload.get("tool_name") not in (*question_write_guard.STRUCTURED_TOOLS, *question_write_guard.PATCH_TOOLS, *GUARDED_TOOLS):
        return ""
    try:
        if not execution.has_input_leases():
            return ""
    except (execution.VerificationError, execution.locking.LockBusy, OSError, ValueError) as error:
        return f"execution input protection: {error}"
    cwd = payload.get("cwd") or os.getcwd()
    targets = question_write_guard.tool_targets(payload)
    if targets:
        targets = [question_write_guard.absolute_path(target, cwd) for target in targets]
    elif payload.get("tool_name") in GUARDED_TOOLS:
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str) or len(command) > 128 * 1024:
            return ""
        tool_name = payload["tool_name"]
        literal_paths = set()
        fragment_cwd = cwd
        # The shared shell extractor resolves literal cd and constant variable
        # assignments. Do not invent a cwd after an unresolved directory change.
        for fragment in question_write_guard.ordered_fragments(command, tool_name, sys.modules[__name__]):
            raw_tokens = question_write_guard.SHELL_TOKEN.findall(fragment)
            tokens = [unquote(token) for token in raw_tokens]
            if not tokens:
                continue
            name = tokens[0].casefold()
            if name in ("pushd", "popd", "push-location", "pop-location"):
                return ""
            if name in ("cd", "chdir", "set-location", "sl"):
                arguments = [token for token in tokens[1:] if not token.startswith("-") and token.casefold() != "/d"]
                if not arguments or any(char in arguments[0] for char in "$`*?[]%~"):
                    return ""
            fragment_cwd = question_write_guard.changed_directory(tokens, fragment_cwd)
            folded = [token.casefold() for token in tokens]
            for index, raw in enumerate(raw_tokens):
                single_quoted = raw.startswith("'") and raw.endswith("'")
                double_quoted = raw.startswith('"') and raw.endswith('"') and not any(char in raw for char in "$`")
                literal_flag = index > 0 and folded[index - 1] == "-literalpath" and (single_quoted or not any(char in raw for char in "$`"))
                quoted_path = (single_quoted or double_quoted) and tool_name != "PowerShell"
                if quoted_path or literal_flag:
                    literal_paths.add(question_write_guard.absolute_path(tokens[index], fragment_cwd))
            if question_write_guard.python_consumer(fragment, sys.modules[__name__]) and "-c" in tokens:
                index = tokens.index("-c") + 1
                if index < len(tokens):
                    python_paths, python_trees = question_write_guard.python_targets(tokens[index])
                    literal_paths.update(question_write_guard.absolute_path(path, fragment_cwd)
                                         for path in [*python_paths, *python_trees])
            if name in ("rename-item", "ren"):
                source = ""
                for flag in ("-literalpath", "-path"):
                    if flag in folded and folded.index(flag) + 1 < len(tokens):
                        source = tokens[folded.index(flag) + 1]
                        break
                values = question_write_guard.positional_arguments(tokens[1:])
                source = source or (values[0] if values else "")
                new_name = ""
                if "-newname" in folded and folded.index("-newname") + 1 < len(tokens):
                    new_name = tokens[folded.index("-newname") + 1]
                elif len(values) > 1:
                    new_name = values[-1]
                if source and new_name and os.path.basename(new_name) == new_name:
                    source_path = question_write_guard.absolute_path(source, fragment_cwd)
                    destination = os.path.join(os.path.dirname(source_path), new_name)
                    source_literal = source_path in literal_paths or not any(char in source for char in "$`*?[]%~")
                    name_literal = question_write_guard.absolute_path(new_name, fragment_cwd) in literal_paths or not any(char in new_name for char in "$`%~")
                    if source_literal and name_literal:
                        literal_paths.add(destination)
        targets, subtrees = question_write_guard.shell_targets(command, tool_name, cwd, sys.modules[__name__])
        targets.extend(subtrees)
        resolved_targets = []
        for target in targets:
            if target in literal_paths:
                resolved_targets.append(target)
            elif not any(char in target for char in "$`%~"):
                wildcard = next((index for index, char in enumerate(target) if char in "*?[]"), None)
                if wildcard is None:
                    resolved_targets.append(target)
                else:
                    # A static glob may write any descendant of its literal
                    # parent. Protect that prefix without guessing its matches.
                    resolved_targets.append(os.path.dirname(target[:wildcard]) or cwd)
        targets = resolved_targets
    if not targets:
        return ""
    try:
        execution.require_mutation_allowed(targets)
    except (execution.VerificationError, execution.locking.LockBusy, OSError, ValueError) as error:
        return f"execution input protection: {error}"
    return ""


def decide(payload):
    try:
        payload = normalize_shared_payload(payload)
    except SharedAllowOnceError as error:
        return Outcome(deny=f"명령 입력 확인 실패: {error}")
    if payload.get("han_worker"):
        from chef.workers.context import denial

        denied = denial(payload)
        if denied:
            return Outcome(deny=denied)
    if payload.get("han_hermes_worker"):
        from chef.runtimes.hermes.workers import project_denial, worker_denial
        denied = worker_denial(payload) or project_denial(payload)
        if denied:
            return Outcome(deny=denied)
    payload = question_write_guard.resolved_payload(payload)
    outcome = Outcome()
    outcome.deny = execution_write_reason(payload)
    if outcome.deny:
        return outcome
    try:
        outcome.deny = question_write_guard.question_write_reason(payload, sys.modules[__name__])
    except GuardOff as e:
        outcome.messages.append(f"질문 쓰기 가드 확인 실패: {e}")
    if outcome.deny:
        return outcome
    tool_name = payload.get("tool_name")
    if tool_name in WRITE_TOOLS:
        try:
            return decide_write(payload)
        except GuardOff:
            return outcome
    if tool_name not in GUARDED_TOOLS:
        return outcome
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return outcome
    marker_command = contains_marker(command, tool_name)
    entry_context = {}
    entry_marker = MARKER.fullmatch(command.strip())
    if marker_command and not entry_marker:
        from chef.cli import mission_entry as mission_entry

        try:
            if mission_entry.registration(payload.get("cwd") or "", payload.get("han_runtime", "claude")):
                outcome.deny = MARKER_ALONE_REASON
                return outcome
        except (mission_entry.EntryError, GuardOff, OSError, ValueError, TypeError) as exc:
            outcome.deny = f"미션 진입 확인 실패: {exc}"
            return outcome
    if entry_marker and entry_marker.group(1).lower() in SESSION_ACTIONS:
        from chef.cli import mission_entry as mission_entry

        action = entry_marker.group(1).lower()
        mode = "new"
        if action == "resume":
            mode = "resume"
        try:
            entry_context = mission_entry.guard_location(payload, mode, entry_marker.group(2))
        except (mission_entry.EntryError, GuardOff, OSError, ValueError, TypeError) as exc:
            outcome.deny = f"미션 진입 확인 실패: {exc}"
            return outcome
    if not marker_command and payload.get("han_origin_cwd"):
        contexts = question_write_guard.mission_contexts(payload, [], sys.modules[__name__])
        if not contexts:
            return outcome
        located, adapter = contexts[0]
    else:
        try:
            if entry_context:
                located = entry_context["located"]
            else:
                located = find_adapter(payload)
        except AdapterNotFound as e:
            if marker_command:
                outcome.messages.append(f"미션 가드 꺼짐: 어댑터 못 찾음 — {e}")
            return outcome
    problems = []
    adapter = load_adapter(located.adapter_path, problems, runtime=payload.get("han_runtime", "claude"))
    state_dir = state_dir_of(located.project, adapter)
    stale_hours = stale_hours_of(adapter)
    try:
        active = read_active(state_dir)
    except Exception:
        if marker_command:
            raise
        return outcome
    if not marker_command and not participates_in_mission(active, payload):
        return outcome

    if payload.get("han_runtime") == "grok" and payload.get("toolInputTruncated"):
        if marker_command or participates_in_mission(active, payload):
            outcome.deny = "Grok 명령 입력이 잘려 미션 가드를 확인할 수 없음; 짧은 명령으로 나눌 것"
            return outcome
    verdict = judge_command(adapter, command, tool_name, problems, located.project, payload.get("cwd") or located.project)
    participants = None
    if verdict.kind not in ("marker", "mixed"):
        try:
            participants = configured_participants(adapter)
            if participants is not None and consume_shared_allow_once(
                state_dir, active.id, command, payload=payload, participant="chef-mission",
                participants=participants, active=active, config=shared_once_context(adapter),
            ):
                outcome.messages.append(f"공유 1회 허용 사용: {command.strip()}")
                return outcome
        except SharedAllowOnceError as error:
            outcome.deny = f"공유 1회 허용 확인 실패: {error}"
            return outcome
    if verdict.kind != "marker":
        try:
            heartbeat(state_dir, payload)
        except Exception:  # heartbeat 실패로 도구를 막지 않음
            pass
    if verdict.kind == "mixed":
        outcome.deny = verdict.reason
    elif verdict.kind == "marker":
        from chef.runtimes.hermes.external_slots import configured

        if configured(adapter):
            outcome.deny = "external slots require the native Hermes mission guard"
            return outcome
        if is_grok_subagent(payload):
            outcome.deny = (
                "Grok 서브에이전트는 미션 open/resume/close를 실행하지 않음; 부모 세션에서 실행할 것"
            )
            return outcome
        if payload.get("agent_id"):
            if payload.get("han_runtime") == "hermes":
                outcome.deny = "Hermes 서브에이전트는 미션 open/resume/close를 실행하지 않음; 부모 세션에서 실행할 것"
            else:
                outcome.deny = "Subagents cannot open, resume or close a mission; use the coordinator session"
            return outcome
        if not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
            outcome.deny = "미션 마커에 소유 session_id가 없음"
            return outcome
        if entry_context or mission_policy_configured(adapter):
            from chef.cli import mission_entry as mission_entry

            try:
                with mission_entry.active_lock(state_dir):
                    checked_state = state_dir
                    if verdict.marker.group(1).lower() in SESSION_ACTIONS:
                        checked_mode = "resume" if verdict.marker.group(1).lower() == "resume" else "new"
                        checked = mission_entry.guard_location(payload, checked_mode, verdict.marker.group(2))
                        if checked:
                            checked_state = state_dir_of(checked["located"].project, checked["adapter"])
                            if mission_entry.path_key(checked_state) != mission_entry.path_key(state_dir):
                                raise mission_entry.EntryError(
                                    "mission entry state directory changed before the marker"
                                )
                    current_adapter = load_adapter(located.adapter_path, runtime=payload.get("han_runtime", "claude"))
                    if current_adapter != adapter:
                        raise mission_entry.EntryError("mission policy adapter changed before the marker")
                    outcome.deny = mission_policy_denial(current_adapter, payload)
                    if outcome.deny:
                        return outcome
                    handle_marker(
                        verdict.marker,
                        checked_state,
                        payload["session_id"],
                        outcome,
                        str(payload.get("transcript_path") or ""),
                        stale_hours,
                        payload.get("han_runtime", "claude"),
                    )
            except (mission_entry.EntryError, GuardOff, OSError, ValueError, TypeError, RuntimeError) as exc:
                outcome.deny = f"미션 진입 확인 실패: {exc}"
        else:
            handle_marker(
                verdict.marker,
                state_dir,
                payload["session_id"],
                outcome,
                str(payload.get("transcript_path") or ""),
                stale_hours,
                "hermes" if payload.get("han_runtime") == "hermes" else "",
            )
    elif verdict.kind == "rule":
        try:
            apply_rule(payload, command, verdict, state_dir, problems, outcome, stale_hours, participants is not None)
        except Exception as e:  # 규칙에 걸린 뒤의 오류는 허용이 아니라 거부
            outcome.deny = f"{verdict.reason} (가드 상태 확인 실패라 허용 안 함: {type(e).__name__}: {e})"
    elif problems:
        report_problems_while_active(state_dir, problems, outcome)
    return outcome


def render_output(outcome):
    output = {}
    specific = {}
    if outcome.messages:
        text = " / ".join(outcome.messages)
        output["systemMessage"] = text
        specific["additionalContext"] = f"미션 가드: {text}"
    if outcome.deny:
        specific["permissionDecision"] = "deny"
        specific["permissionDecisionReason"] = outcome.deny
    if specific:
        output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", **specific}
    return output


def main(runtime=""):
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        payload = payload if isinstance(payload, dict) else {}
        payload = normalize_payload(payload, runtime)
        log_payload(payload)
        outcome = decide(payload)
        if payload.get("han_worker"):
            from chef.workers.context import acknowledge

            acknowledge(payload, outcome.deny)
        if payload.get("han_hermes_worker"):
            from chef.runtimes.hermes.workers import acknowledge_guard
            acknowledge_guard(payload, outcome.deny)
    except GuardOff as e:
        outcome = Outcome(deny=f"Codex 워커 가드 확인 실패: {e}") if os.environ.get("CHEF_HERMES_WORKER") or os.environ.get("CHEF_WORKER_CONTEXT") else Outcome(messages=[f"미션 가드 꺼짐: {e}"])
    except Exception as e:  # 가드 오류로 도구를 막지 않음
        outcome = Outcome(deny=f"Codex 워커 가드 확인 실패: {e}") if os.environ.get("CHEF_HERMES_WORKER") or os.environ.get("CHEF_WORKER_CONTEXT") else Outcome(messages=[f"미션 가드 꺼짐: 내부 오류 ({e})"])
    output = render_output(outcome)
    if os.environ.get("CHEF_WORKER_CONTEXT") and outcome.deny:
        output.update(decision="deny", reason=outcome.deny)
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=True))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
