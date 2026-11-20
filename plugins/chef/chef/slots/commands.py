"""slots / commands."""

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from chef.cli.slots_parser import build_parser
from chef.guards import mission as mission_guard
from chef.guards.slot_binding import entry_matches, record_slot_registry
from chef.infrastructure.configuration import locate, read_adapter
from chef.infrastructure.errors import UsageError
from chef.infrastructure.locking import (
    LOCK_POLL_SECONDS,
    LOCK_UNREADABLE_SECONDS,
    THREAD_LOCKS,
    THREAD_LOCKS_GUARD,
    FileLock,
    LockBusy,
    process_alive,
    thread_lock_of,
)
from chef.infrastructure.paths import (
    REQUEST_PATHS,
    inside_root,
    is_link,
    normalize,
    request_realpath,
    request_realpaths,
    same_path,
)
from chef.infrastructure.process import (
    AGENT_SHELL_ENV,
    AGENT_TOOL_ENV,
    PARENT_SESSION_ENV,
    child_env,
    decode,
    launch_env,
)
from chef.infrastructure.vcs import (
    BASELINE_LINE,
    CHURN_TOKEN,
    CHURN_WILDCARDS,
    CLIENT_NAME_LINE,
    DEPOT_LINE_PATH,
    MAIN_KEEP_DESCRIPTION,
    NO_RECONCILE,
    SLOT_DESCRIPTION,
    SLOT_MARKER,
    WINDOWS_DRIVE,
    Completed,
    NoVcs,
    Perforce,
    build_vcs,
    churn_entries,
    churn_pattern,
    get_field,
    p4config_client,
    set_field,
    spec_pattern,
)
from chef.runtimes.claude.session import claude_memory_dir, project_dir_name
from chef.runtimes.grok import injection as grok_inject
from chef.runtimes.grok import transcript as grok_transcript
from chef.runtimes.launch import (
    grok_leader_record_path,
    grok_leader_socket,
    launch_editor,
    launch_mission,
    ensure_editor,
    ensure_launcher,
    mission_command,
    terminal_backend,
    wait_foreground,
    read_grok_leader_socket,
    write_grok_leader_record,
    wt_argument,
    wt_child_command,
    wt_command,
)
from chef.slots.configuration import (
    CODEX_AGENT_NAME,
    CODEX_AGENT_RESERVED,
    TOML_BARE_KEY,
    VARIABLE,
    Config,
    expand,
    toml_key,
    toml_string,
)
from chef.slots.contracts import (
    BUSY,
    CLAUDE_ENTRY_TYPES,
    FAILED,
    HOOKS_DIR,
    KEEP_DESCRIPTION,
    LAUNCH_PREPARE,
    MISSION_FIELD,
    NAME_PATTERN,
    OK,
    PREVIEW_LIMIT,
    RESUME_PROMPT,
    ROW_FIELDS,
    RUNTIME_PROBE_LINES,
    RUNTIMES,
    TERMINAL_RUNTIMES,
    SESSION_NAME_LIMIT,
    STATE_CREATING,
    STATE_FAILED,
    STATE_READY,
    STATE_REFRESH_FAILED,
    SYNC_STEP,
    USAGE,
)
from chef.slots.execution import EXCLUSIVE_LOCK_TIMEOUT, Runner
from chef.slots.models import Outcome, Step, StepResult
from chef.slots.storage import ROWS_LOCK_TIMEOUT, Rows


def row_state(row):
    return row.get("state", "")


def format_seconds(results):
    return ", ".join(f"{result.id} {result.seconds:.0f}s" for result in results)


def first_failure(results):
    return next((result for result in results if not result.ok), None)


def print_baseline(config):
    if config.baseline:
        origin = config.baseline_origin or f"메인 워크스페이스 {config.vcs.main_client} 의 have"
        print(f"기준 CL: {config.baseline} ({origin})")


def describe_steps(steps):
    lines = []
    for step in steps:
        flags = " exclusive" if step.exclusive else ""
        where = f" (cwd {step.cwd})" if step.cwd else ""
        lines.append(f"  group {step.group}{flags} {step.id}{where}: {step.command}")
    return lines


def split_at_sync(steps):
    sync_group = next((step.group for step in steps if step.id == SYNC_STEP), None)
    if sync_group is None:
        return steps, []
    return [step for step in steps if step.group <= sync_group], [
        step for step in steps if step.group > sync_group
    ]


def describe_refresh(config, name, steps, keep_local):
    vcs, client = config.vcs, config.client_of(name)
    head, tail = split_at_sync(steps) if keep_local else (steps, [])
    keep_description = KEEP_DESCRIPTION.format(name=name)
    keep = (
        [f"  keep-local: {command}" for command in vcs.describe_keep_local(client, keep_description)]
        if keep_local
        else []
    )
    churn = [f"  churn 되돌림: {command}" for command in vcs.describe_churn(client)]
    resolve = (
        [f"  resolve (sync 뒤): {command}" for command in vcs.describe_resolve(client)] if keep_local else []
    )
    return [*keep, *churn, *describe_steps(head), *resolve, *describe_steps(tail)]


def registry_warning(config):
    choice = mission_guard.resolve_registry(config.runtime, project=config.root)
    directory = choice.path
    if not directory:
        return (
            "레지스트리를 찾지 못해 roots 확인 못 함 "
            f"({mission_guard.registry_missing_message(config.runtime, project=config.root)})"
        )
    if not os.path.isdir(directory):
        return f"레지스트리 폴더 없음: {directory} (출처 {choice.source})"
    try:
        entries = mission_guard.load_registry(directory)
    except mission_guard.GuardOff as e:
        return f"레지스트리를 읽지 못해 roots 확인 못 함: {e}"
    sample = config.slot_path("x")
    if any(entry_matches(sample, entry, slot_name="x", allow_basename=True) for entry in entries):
        return ""
    return f"레지스트리 roots 에 {config.root}/* 가 없음 — 슬롯에서 띄운 세션이 어댑터를 못 찾음 (출처 {choice.source}: {directory})"


def read_mission_fields(slot_state, mission_id):
    fields = {"상태": "-", "현재 단계": "-"}
    path = os.path.join(slot_state, mission_id, "mission.md")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                match = MISSION_FIELD.match(line)
                if match:
                    fields[match.group(1)] = match.group(2) or "-"
    except OSError:
        pass
    return fields


def active_of(config, name):
    return mission_guard.read_active(config.slot_state_dir(name))


def stale_note(config, name, active):
    # 끊김 의심이면 안내 문구, 아니면 "". 판정 실패는 조용히 무시(상태 출력이 깨지지 않게)
    if not active or not active.id or active.corrupt:
        return ""
    try:
        state = mission_guard.staleness(
            config.slot_state_dir(name), active, getattr(config, "stale_hours", 0)
        )
    except Exception:
        return ""
    return mission_guard.stale_text(state) if state.stale else ""


def active_text(active):
    if active.corrupt:
        return "손상"
    return f"{active.id}@{active.session[:8]}" if active.session else active.id


def remove_tree(path):
    def make_writable(function, target, *_):
        os.chmod(target, 0o700)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def render_table(header, rows):
    widths = [max(display_width(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    lines = []
    for row in [header, *rows]:
        cells = [str(cell) + " " * (width - display_width(str(cell))) for cell, width in zip(row, widths)]
        lines.append("  ".join(cells).rstrip())
    return lines


def state_refusal(name, state, allow_refresh_failed):
    if state == STATE_READY:
        return ""
    if allow_refresh_failed and state.startswith(STATE_REFRESH_FAILED):
        return ""
    if not state:
        return f"슬롯 {name} 이 slots.tsv 에 없음 — create --resume {name} 으로 준비를 마쳐야 함"
    if state == STATE_CREATING:
        return f"슬롯 {name} 의 state 가 creating — create --resume {name} 으로 이어서 마쳐야 함"
    if state.startswith(STATE_REFRESH_FAILED):
        return f"슬롯 {name} 의 state 가 {state} — refresh 로 복구해야 함 (run 은 --refresh)"
    if state.startswith(STATE_FAILED):
        return f"슬롯 {name} 의 state 가 {state} — create --resume {name} 으로 준비를 다시 돌려야 함"
    return f"슬롯 {name} 의 state 가 {state} — ready 가 아님"


def slot_refusal(config, name):
    slot = config.slot_path(name)
    client = config.client_of(name)
    problem = config.path_problem(name)
    if problem:
        return problem
    if not os.path.isdir(slot):
        return f"슬롯 폴더 없음: {slot}"
    if config.vcs.has_workspaces and not config.vcs.client_exists(client):
        return f"작업 공간 없음: {client}"
    return config.vcs.identity_problem(name, client, slot)


def idle_refusal(config, name, allow_refresh_failed):
    state = row_state(Rows(config.rows_path).find(name))
    refusal = state_refusal(name, state, allow_refresh_failed) or slot_refusal(config, name)
    if refusal:
        return refusal
    active = active_of(config, name)
    if active.id:
        stale = stale_note(config, name, active)
        hint = f" ({stale}; {mission_guard.FORCE_CLOSE_HINT})" if stale else ""
        return f"슬롯 {name} 에서 미션 {active_text(active)} 진행 중{hint}"
    return ""


def preview(items):
    shown = list(items[:PREVIEW_LIMIT])
    if len(items) > PREVIEW_LIMIT:
        shown.append(f"… 외 {len(items) - PREVIEW_LIMIT}개")
    return shown


def churn_notes(skipped):
    return [f"엔진이 다시 쓰는 파일 {skipped}개는 검사에서 뺌"] if skipped else []


def local_change_reasons(config, client, edits_only, consequence):
    changes, skipped = config.vcs.local_changes(client, edits_only)
    notes = churn_notes(skipped)
    if not changes:
        return [], notes
    reasons = [
        f"로컬 변경 {len(changes)}개 — {consequence}. 버리려면 --discard-local",
        *(f"  {line}" for line in preview(changes)),
    ]
    return reasons, notes


def workspace_warning(config, name):
    current = config.vcs.workspace_name_in(config.slot_path(name))
    client = config.client_of(name)
    if not current or current.casefold() == client.casefold():
        return ""
    return f"p4 info 의 작업 공간이 {current} (기대 {client}) — P4CLIENT 환경변수·레지스트리가 .p4config 를 이김, 쓰기 명령은 p4 -c"


def create_one(runner, name, steps, resume):
    config, vcs = runner.config, runner.config.vcs
    slot = config.slot_path(name)
    client = config.client_of(name)
    with FileLock(config.slot_lock_path(name), f"슬롯 {name}"):
        row = runner.rows.find(name)
        if resume:
            if row_state(row) == STATE_READY:
                return Outcome(name, "skipped", "이미 ready — 최신화는 refresh")
            refusal = slot_refusal(config, name)
            if refusal:
                return Outcome(name, "failed", f"이어받을 수 없음: {refusal}")
        else:
            problem = config.path_problem(name)
            if problem:
                return Outcome(name, "failed", problem)
            if os.path.exists(slot):
                return Outcome(
                    name, "skipped", f"폴더가 이미 있음: {slot} (준비가 덜 끝났으면 create --resume {name})"
                )
            if vcs.client_exists(client):
                return Outcome(
                    name, "skipped", f"작업 공간이 이미 있음: {client} (이어받으려면 create --resume {name})"
                )
        begin = time.monotonic()
        today = date.today().isoformat()
        marked = {"path": slot.replace(os.sep, "/"), "client": client, "state": STATE_CREATING}
        if not row:
            marked.update(created=today, synced="-")
        runner.rows.upsert(name, **marked)
        step = "workspace"
        try:
            if not resume:
                vcs.create_workspace(name, client, slot)
                step = "folder"
                os.makedirs(slot)
                step = "config"
                vcs.write_config(client, slot)
            step = "prepare"
            results = runner.run_steps(name, steps)
        except (RuntimeError, OSError):
            runner.rows.upsert(name, state=STATE_FAILED + step)
            raise
        failure = first_failure(results)
        if failure:
            runner.rows.upsert(name, state=STATE_FAILED + failure.id)
            return Outcome(name, "failed", failure.failure_text())
        try:
            record_slot_registry(config, name)
        except OSError as exc:
            runner.rows.upsert(name, state=STATE_FAILED + "binding")
            return Outcome(name, "failed", f"슬롯 레지스트리 바인딩을 쓰지 못함: {exc}")
        runner.rows.upsert(name, synced=today, state=STATE_READY)
        outcome = Outcome(
            name,
            "resumed" if resume else "created",
            f"{time.monotonic() - begin:.0f}s ({format_seconds(results)})",
        )
        outcome.warnings.append(workspace_warning(config, name))
        failure_text = before_run_failure(config, name, LAUNCH_PREPARE, "")
        if failure_text:
            return Outcome(
                name,
                "failed",
                f"슬롯은 {outcome.state} 이지만 beforeRun 실패: {failure_text}",
                warnings=outcome.warnings,
            )
        return outcome


def guarded(function, runner, name, *args):
    try:
        return function(runner, name, *args)
    except (RuntimeError, OSError) as e:
        return Outcome(name, "failed", str(e))


def report(outcomes, warnings=()):
    counts = {}
    for outcome in outcomes:
        counts[outcome.state] = counts.get(outcome.state, 0) + 1
    print("요약: " + (", ".join(f"{state} {count}" for state, count in counts.items()) or "대상 없음"))
    for outcome in outcomes:
        print(f"  {outcome.name}: {outcome.state}" + (f" — {outcome.detail}" if outcome.detail else ""))
        for note in outcome.notes:
            print(f"    {note}")
    for warning in filter(None, [*warnings, *(w for outcome in outcomes for w in outcome.warnings)]):
        print(f"경고: {warning}")
    return FAILED if any(outcome.failed for outcome in outcomes) else OK


def unique_names(config, names):
    return list(dict.fromkeys(config.check_name(name) for name in names))


def require_names(config, names):
    if not names:
        raise UsageError("슬롯 이름이 필요함")
    return unique_names(config, names)


def cmd_create(config, args):
    names = unique_names(config, args.names)
    if args.resume and not names:
        raise UsageError("create --resume 은 슬롯 이름이 필요함")
    if not names:
        names = [name for name in config.names if not os.path.exists(config.slot_path(name))]
    if not names:
        print("만들 슬롯 없음 (slots.names 가 모두 있음)")
        return OK
    for name in names:
        config.check_requirements(name)
    plans = {name: config.build_steps(name) for name in names}
    before_run_plans = {name: config.before_run_steps(name, LAUNCH_PREPARE, "") for name in names}
    print_baseline(config)
    if args.dry_run:
        return dry_create(config, plans, before_run_plans, args.resume)
    runner = Runner(config)
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes = list(
            pool.map(lambda name: guarded(create_one, runner, name, plans[name], args.resume), names)
        )
    created = any(outcome.state in ("created", "resumed") for outcome in outcomes)
    return report(outcomes, [registry_warning(config)] if created else [])


def dry_create(config, plans, before_run_plans, resume):
    rows = Rows(config.rows_path)
    for name, steps in plans.items():
        slot = config.slot_path(name)
        client = config.client_of(name)
        if resume:
            print(f"[{name}] create --resume (dry-run) 폴더 {slot}")
            if row_state(rows.find(name)) == STATE_READY:
                print("  이미 ready — 건너뜀")
                continue
            refusal = slot_refusal(config, name)
            if refusal:
                print(f"  거부: {refusal}")
                continue
            print("  작업 공간·폴더 만들기는 건너뛰고 prepare 전체를 다시 실행")
        else:
            exists = "있음 — 건너뜀" if os.path.exists(slot) else "없음"
            print(f"[{name}] create (dry-run) 폴더 {slot} {exists}")
            for line in config.vcs.describe_create(name, client, slot):
                print(f"  {line}")
        for line in describe_steps(steps):
            print(line)
        for line in describe_steps(before_run_plans[name]):
            print(line)
    return OK


def refresh_one(runner, name, steps, keep_local, discard_local, with_before_run=True):
    config, vcs = runner.config, runner.config.vcs
    client = config.client_of(name)
    with FileLock(config.slot_lock_path(name), f"슬롯 {name}"):
        refusal = idle_refusal(config, name, allow_refresh_failed=True)
        if refusal:
            return Outcome(name, "failed", f"갱신 거부: {refusal}")
        opened = vcs.opened(client)
        churn_opened = [line for line in opened if vcs.is_churn(line)]
        if churn_opened:
            return Outcome(
                name,
                "failed",
                "엔진이 다시 쓰는 파일(slots.churn)이 열려 있음 — 갱신 거부",
                churn_opened[:PREVIEW_LIMIT],
            )
        if opened and not keep_local:
            return Outcome(
                name,
                "failed",
                f"열린 파일 {len(opened)}개 — 갱신 거부 (--keep-local 로 보관하고 진행)",
                opened[:PREVIEW_LIMIT],
            )
        notes = []
        if keep_local:
            changes, skipped = vcs.local_changes(client, True)
            notes = churn_notes(skipped)
            if changes:
                changelist, count = vcs.keep_local(client, KEEP_DESCRIPTION.format(name=name), changes)
                notes.append(f"로컬 변경 보관: 보관 CL {changelist} 에 열리지 않은 변경 파일 {count}개를 엶")
        elif not discard_local:
            reasons, notes = local_change_reasons(
                config, client, True, "열리지 않은 채 바뀐 파일이 sync 로 덮임"
            )
            if reasons:
                return Outcome(name, "failed", "갱신 거부", [*reasons, *notes])
        try:
            vcs.restore_churn(client)
        except RuntimeError:
            runner.rows.upsert(name, state=STATE_REFRESH_FAILED + "churn")
            raise
        head, tail = split_at_sync(steps) if keep_local else (steps, [])
        stage = "prepare"
        conflicts = []
        try:
            results = runner.run_steps(name, head)
            failure = first_failure(results)
            if keep_local and not failure:
                stage = "resolve"
                conflicts = vcs.resolve_conflicts(client)
                stage = "prepare"
                if not conflicts:
                    results += runner.run_steps(name, tail)
                    failure = first_failure(results)
        except (RuntimeError, OSError):
            runner.rows.upsert(name, state=STATE_REFRESH_FAILED + stage)
            raise
        if conflicts:
            runner.rows.upsert(name, state=STATE_REFRESH_FAILED + "resolve")
            detail = f"충돌 {len(conflicts)}개 남음 — 직접 resolve 한 뒤 refresh --keep-local 다시"
            return Outcome(name, "failed", detail, [*preview(conflicts), *notes])
        if failure:
            runner.rows.upsert(name, state=STATE_REFRESH_FAILED + failure.id)
            return Outcome(name, "failed", failure.failure_text(), notes)
        runner.rows.upsert(name, synced=date.today().isoformat(), state=STATE_READY)
        outcome = Outcome(name, "refreshed", format_seconds(results), notes)
        outcome.warnings.append(workspace_warning(config, name))
        if with_before_run:
            failure_text = before_run_failure(config, name, LAUNCH_PREPARE, "")
            if failure_text:
                return Outcome(
                    name,
                    "failed",
                    f"슬롯은 refreshed 이지만 beforeRun 실패: {failure_text}",
                    notes,
                    outcome.warnings,
                )
        return outcome


def reject_conflicting_local_flags(args):
    if args.keep_local and args.discard_local:
        raise UsageError("--keep-local 과 --discard-local 은 같이 못 씀")


def sync_main_workspace(config, names, main_keep_local, dry_run):
    vcs = config.vcs
    if not vcs.has_workspaces:
        return None
    if all(idle_refusal(config, name, allow_refresh_failed=True) for name in names):
        return None
    main = vcs.main_client
    with (
        contextlib.nullcontext()
        if dry_run
        else FileLock(config.main_lock_path, "메인 sync", EXCLUSIVE_LOCK_TIMEOUT)
    ):
        head = vcs.head()
        pending = vcs.sync_preview(head)
        if not pending:
            print(f"메인 sync: 메인 워크스페이스 {main} 은 이미 head {head} — 생략")
            return None
        have = vcs.baseline()
        overwritten = vcs.unopened_edits(pending)
        if dry_run:
            print(f"[메인 {main}] sync (dry-run) have {have} → head {head}, 바뀔 파일 {len(pending)}개")
        if overwritten and not main_keep_local:
            detail = (
                f"메인 워크스페이스 {main} 에 열지 않고 고친 파일 {len(overwritten)}개가 sync 로 덮임 — "
                "메인 sync·슬롯 갱신 안 함 (--main-keep-local 로 메인 보관 CL 에 열고 진행)"
            )
            return report([Outcome("main", "failed", f"메인 sync 거부: {detail}", preview(overwritten))])
        if dry_run:
            if overwritten:
                print(f"  열지 않고 고쳐 sync 로 덮일 파일 {len(overwritten)}개 — 메인 보관 CL 에 열고 진행")
                print("\n".join(f"    {line}" for line in preview(overwritten)))
            print("\n".join(f"  {line}" for line in vcs.describe_main_sync(head, bool(overwritten))))
            config.baseline = head
            config.baseline_origin = f"메인 sync 뒤 head {head} 기준, 지금 메인 have 는 {have}"
            return None
        notes = []
        if overwritten:
            changelist, count = vcs.keep_local(main, MAIN_KEEP_DESCRIPTION, overwritten)
            notes.append(f"메인 보관 CL {changelist} 에 열리지 않은 변경 파일 {count}개를 엶")
        vcs.sync_main(head)
        conflicts = vcs.resolve_conflicts(main)
        if conflicts:
            detail = f"메인 충돌 {len(conflicts)}개 남음 — 직접 resolve 한 뒤 다시 (슬롯 갱신 안 함)"
            return report([Outcome("main", "failed", detail, [*preview(conflicts), *notes])])
        print(f"메인 sync: {have} → {head} (파일 {len(pending)}개)")
        for note in notes:
            print(note)
    return None


def cmd_refresh(config, args):
    names = require_names(config, args.names)
    reject_conflicting_local_flags(args)
    config.check_refresh(names, args.keep_local)
    for name in names:
        config.check_requirements(name)
    code = sync_main_workspace(config, names, args.main_keep_local, args.dry_run)
    if code is not None:
        return code
    plans = {name: config.refresh_steps(name, args.keep_local) for name in names}
    before_run_plans = {name: config.before_run_steps(name, LAUNCH_PREPARE, "") for name in names}
    print_baseline(config)
    if args.dry_run:
        for name, steps in plans.items():
            print(f"[{name}] refresh (dry-run)")
            refusal = idle_refusal(config, name, allow_refresh_failed=True)
            if refusal:
                print(f"  거부: {refusal}")
                continue
            print(
                "\n".join(
                    [
                        *describe_refresh(config, name, steps, args.keep_local),
                        *describe_steps(before_run_plans[name]),
                    ]
                )
            )
        return OK
    runner = Runner(config)
    outcomes = [
        guarded(refresh_one, runner, name, plans[name], args.keep_local, args.discard_local) for name in names
    ]
    return report(outcomes)


def resume_refusal(config, name):
    state = row_state(Rows(config.rows_path).find(name))
    refusal = state_refusal(name, state, allow_refresh_failed=False) or slot_refusal(config, name)
    if refusal:
        return refusal, ""
    mission_id = active_of(config, name).id
    if not mission_id:
        return f"슬롯 {name} 에 진행 중 미션 없음 — run {name} <작업> 으로 새로 시작", ""
    return "", mission_id


def session_runtime(folder, session, strict=False):
    from chef.mission import state as mission_state

    if not mission_state.SESSION_ID.fullmatch(session):
        return ""
    sessions_path = os.path.join(folder, mission_guard.SESSIONS_FILE)
    rows = []
    if os.path.isfile(sessions_path):
        rows = mission_state.read_table(sessions_path, mission_guard.SESSIONS_HEADER)
    transcript = next(
        (
            row["transcript"]
            for row in reversed(rows)
            if row["session"] == session and os.path.isfile(row["transcript"])
        ),
        "",
    )
    if not transcript:
        home = os.path.expanduser("~")
        if strict:
            candidates = [
                mission_state.search_codex(session, home, require_unique=True),
                mission_state.search_claude(session, home, require_unique=True),
                grok_transcript.find_transcript(session, home, require_unique=True),
            ]
            found = {os.path.normcase(os.path.realpath(path)): path for path in candidates if path}
            if len(found) > 1:
                raise UsageError("ambiguous session runtime transcript fallback")
            transcript = next(iter(found.values()), "")
        else:
            transcript = (
                mission_state.search_codex(session, home)
                or mission_state.search_claude(session, home)
                or grok_transcript.find_transcript(session, home)
            )
    if not transcript:
        return ""
    if grok_transcript.is_transcript(transcript):
        return "grok"
    with open(transcript, encoding="utf-8-sig") as stream:
        for _, line in zip(range(RUNTIME_PROBE_LINES), stream):
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "session_meta":
                return "codex"
            if entry.get("type") in CLAUDE_ENTRY_TYPES or entry.get("sessionId") == session:
                return "claude"
    return ""


def resume_session_id(config, name, mission_id, new_session=False):
    session = active_of(config, name).session
    resume_session = ""
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session):
        resume_session = session
    if new_session:
        resume_session = ""
    if resume_session:
        from chef.mission import state as mission_state

        try:
            runtime = session_runtime(os.path.join(config.slot_state_dir(name), mission_id), session)
        except (OSError, ValueError, mission_state.StateError) as exc:
            raise UsageError(f"슬롯 {name} 세션 기록 확인 실패: {exc}") from exc
        if not runtime:
            raise UsageError(
                f"슬롯 {name} 의 기존 세션 {session} 기록을 못 찾음 — --new-session 으로 새 {config.runtime} 세션에서 이어받을 것"
            )
        if runtime == config.runtime:
            return resume_session
        else:
            raise UsageError(
                f"슬롯 {name} 의 기존 세션은 {runtime} — 해당 런타임의 슬롯 스크립트로 이어받거나 --new-session 으로 새 {config.runtime} 세션에서 이어받을 것"
            )
    return ""


def resume_command(config, name, mission_id, new_session=False):
    resume_session = resume_session_id(config, name, mission_id, new_session)
    return run_command(config, name, RESUME_PROMPT, f"{mission_id} 이어서", resume_session)


def run_command(config, *args):
    # Preserve the public WT builder/patch seam while selecting native backends.
    return wt_command(config, *args) if terminal_backend(config) == "wt" else mission_command(config, *args)


def describe_launch(config, command):
    text = subprocess.list2cmdline(command) if terminal_backend(config) == "wt" else shlex.join(command)
    return ("wt: " if terminal_backend(config) == "wt" else f"{terminal_backend(config)}: ") + text


def before_run_failure(config, name, launch_mode, session_id):
    results = Runner(config, "beforeRun").run_steps(
        name, config.before_run_steps(name, launch_mode, session_id)
    )
    failure = first_failure(results)
    if failure:
        return failure.failure_text()
    return ""


def run_before_run(config, name, launch_mode, session_id):
    failure_text = before_run_failure(config, name, launch_mode, session_id)
    if failure_text:
        print(f"[{name}] beforeRun 실패: {failure_text}")
        return False
    return True


def cmd_run_resume(config, args, name):
    if args.task:
        raise UsageError("--resume 에는 작업을 줄 수 없음")
    if args.refresh or args.keep_local or args.discard_local or args.main_keep_local:
        raise UsageError(
            "--resume 은 --refresh·--keep-local·--discard-local·--main-keep-local 과 같이 못 씀 — 진행 중 미션의 열린 파일을 건드리면 안 됨"
        )
    config.check_requirements(name)
    if args.editor and not (config.editor or getattr(config, "editor_argv", [])):
        raise UsageError("slots.editor 또는 editorArgv 가 없음")
    if args.editor:
        ensure_editor(config, name)
    refusal, mission_id = resume_refusal(config, name)
    if refusal:
        print(f"거부: {refusal}")
        return FAILED
    session_id = resume_session_id(config, name, mission_id, args.new_session)
    command = run_command(config, name, RESUME_PROMPT, f"{mission_id} 이어서", session_id)
    if args.dry_run:
        print_baseline(config)
        before_run = config.before_run_steps(name, "resume", session_id)
        if before_run:
            print("\n".join(describe_steps(before_run)))
        if args.editor:
            print(f"editor: {ensure_editor(config, name)}")
        print(describe_launch(config, command))
        return OK
    ensure_launcher(config)
    print_baseline(config)
    with FileLock(config.slot_lock_path(name), f"슬롯 {name}"):
        refusal, mission_id = resume_refusal(config, name)
        if refusal:
            print(f"거부: {refusal}")
            return FAILED
        session_id = resume_session_id(config, name, mission_id, args.new_session)
        command = run_command(config, name, RESUME_PROMPT, f"{mission_id} 이어서", session_id)
        if not run_before_run(config, name, "resume", session_id):
            return FAILED
        config.check_codex_agent_files(name)
        if args.editor:
            launch_editor(config, name)
        try:
            process = launch_mission(config, name, command)
        except OSError as e:
            print(f"터미널 실행 실패: {e}")
            return FAILED
    if wait_foreground(config, process):
        return FAILED
    print(f"요약: {name} 에서 미션 {mission_id} 이어서 열기 요청 — {subprocess.list2cmdline(command)}")
    return OK


def cmd_run(config, args):
    if config.runtime == "hermes":
        raise UsageError("Hermes는 현재 세션의 chef_slot 도구로 슬롯을 선택한다. 새 터미널을 열지 않는다")
    name = config.check_name(args.name)
    if args.resume:
        return cmd_run_resume(config, args, name)
    if args.new_session:
        raise UsageError("--new-session 은 --resume 과 같이 써야 함")
    config.check_requirements(name)
    task = " ".join(args.task).strip()
    if not task:
        raise UsageError("run <name> <작업...> 에 작업이 필요함")
    if '"' in task or "\n" in task or "\r" in task:
        raise UsageError('작업 문자열에 큰따옴표(")·개행을 쓸 수 없음 — 바꿔 쓰기')
    if args.editor and not (config.editor or getattr(config, "editor_argv", [])):
        raise UsageError("slots.editor 또는 editorArgv 가 없음")
    reject_conflicting_local_flags(args)
    if args.main_keep_local and not args.refresh:
        raise UsageError("--main-keep-local 은 --refresh 와 같이 써야 함")
    if args.refresh:
        config.check_refresh([name], args.keep_local)
    if args.editor:
        ensure_editor(config, name)
    command = run_command(config, name, task)
    if args.dry_run:
        refusal = idle_refusal(config, name, allow_refresh_failed=args.refresh)
        if refusal:
            print(f"거부: {refusal}")
            return FAILED
        if args.refresh:
            code = sync_main_workspace(config, [name], args.main_keep_local, dry_run=True)
            if code is not None:
                return code
        refresh_plan = []
        if args.refresh:
            refresh_plan = config.refresh_steps(name, args.keep_local)
        print_baseline(config)
        if args.refresh:
            print(f"[{name}] refresh (dry-run)")
            print("\n".join(describe_refresh(config, name, refresh_plan, args.keep_local)))
        before_run = config.before_run_steps(name, "new", "")
        if before_run:
            print("\n".join(describe_steps(before_run)))
        if args.editor:
            print(f"editor: {ensure_editor(config, name)}")
        print(describe_launch(config, command))
        return OK
    ensure_launcher(config)
    if args.refresh:
        code = sync_main_workspace(config, [name], args.main_keep_local, dry_run=False)
        if code is not None:
            return code
    refresh_plan = []
    if args.refresh:
        refresh_plan = config.refresh_steps(name, args.keep_local)
    print_baseline(config)
    if args.refresh:
        outcome = guarded(
            refresh_one, Runner(config), name, refresh_plan, args.keep_local, args.discard_local, False
        )
        if outcome.failed:
            return report([outcome])
        for warning in filter(None, outcome.warnings):
            print(f"경고: {warning}")
    with FileLock(config.slot_lock_path(name), f"슬롯 {name}"):
        refusal = idle_refusal(config, name, allow_refresh_failed=False)
        if refusal:
            print(f"거부: {refusal}")
            return FAILED
        if not run_before_run(config, name, "new", ""):
            return FAILED
        config.check_codex_agent_files(name)
        if args.editor:
            launch_editor(config, name)
        try:
            process = launch_mission(config, name, command)
        except OSError as e:
            print(f"터미널 실행 실패: {e}")
            return FAILED
    if wait_foreground(config, process):
        return FAILED
    print(f"요약: {name} 에서 미션 시작 요청 — {subprocess.list2cmdline(command)}")
    return OK


def current_session_context(project, name, session_id, resume=False, dry_run=False):
    """Prepare a Hermes slot without launching a process or claiming ACTIVE.

    Only a native tool handler may bind the returned directory to its trusted
    task. Opening the mission still goes through the normal guarded marker.
    """
    if not isinstance(session_id, str) or not session_id or any(c in session_id for c in "\x00\t\r\n"):
        raise UsageError("Hermes 작업 세션 ID가 필요함")
    if not isinstance(project, str) or not os.path.isabs(project):
        raise UsageError("프로젝트 절대 경로가 필요함")
    located = locate(project, "hermes")
    config = Config(located, read_adapter(located.adapter_path, "hermes"), "hermes")
    config.check_name(name)
    config.check_requirements(name)
    with (contextlib.nullcontext() if dry_run else FileLock(config.slot_lock_path(name), f"슬롯 {name}")):
        mission_id = ""
        if resume:
            refusal, mission_id = resume_refusal(config, name)
            active = active_of(config, name)
            if not refusal and (active.runtime != "hermes" or active.session != session_id):
                refusal = "기존 Hermes 소유 세션에서 재개할 것. 다른 세션의 ACTIVE를 인계하지 않음"
        else:
            refusal = idle_refusal(config, name, allow_refresh_failed=False)
        if refusal:
            raise UsageError(refusal)
        mode = "resume" if resume else "new"
        steps = config.before_run_steps(name, mode, session_id)
        if not dry_run:
            failed = before_run_failure(config, name, mode, session_id)
            if failed:
                raise UsageError(f"beforeRun 실패: {failed}")
            active = active_of(config, name)
            if active.corrupt or (resume and (active.id != mission_id or active.session != session_id or active.runtime != "hermes")) or (not resume and active.id):
                raise UsageError("beforeRun 중 미션 소유 상태가 바뀌어 현재 세션 진입을 중단함")
        try:
            slot_location = locate(config.slot_path(name), "hermes")
            slot_adapter = read_adapter(slot_location.adapter_path, "hermes")
        except UsageError as exc:
            slot = config.slot_path(name)
            raise UsageError(
                "Hermes 슬롯 어댑터를 찾지 못함: 슬롯 로컬 어댑터 또는 레지스트리 roots를 설정할 것"
                f" (slot={slot}; {exc})"
            ) from exc
        if not same_path(slot_location.project, config.slot_path(name)) or not same_path(
            mission_guard.state_dir_of(slot_location.project, slot_adapter), config.slot_state_dir(name)
        ):
            raise UsageError("슬롯의 어댑터와 stateDir이 현재 작업 대상에 일치하지 않음")
        return {
            "runtime": "hermes", "cwd": os.path.realpath(config.slot_path(name)),
            "project": os.path.realpath(located.project), "slot": name,
            "session_id": session_id, "mission": mission_id, "mode": mode,
            "beforeRun": [step.id for step in steps], "dry_run": bool(dry_run),
        }


def blockers_of(config, name, has_client):
    client = config.client_of(name)
    reasons = []
    active = active_of(config, name)
    if active.id:
        reasons.append(f"ACTIVE 미션 {active_text(active)}")
    if not has_client:
        return reasons
    for label, items in (
        ("열린 파일", config.vcs.opened(client)),
        ("shelved CL", config.vcs.shelved(client)),
        ("pending CL", config.vcs.pending(client)),
    ):
        if items:
            reasons.append(f"{label} {len(items)}개")
            reasons.extend(f"  {item}" for item in items[:PREVIEW_LIMIT])
    return reasons


def remove_one(runner, name, discard_local):
    config, vcs = runner.config, runner.config.vcs
    client = config.client_of(name)
    slot = config.slot_path(name)
    with FileLock(config.slot_lock_path(name), f"슬롯 {name}"):
        row = runner.rows.find(name)
        state = row_state(row)
        if state == STATE_CREATING:
            return Outcome(
                name,
                "failed",
                f"state 가 creating — create 가 돌고 있거나 중단됨. create --resume {name} 으로 마친 뒤 지움",
            )
        if state and state != STATE_READY and not state.startswith(STATE_FAILED):
            return Outcome(name, "failed", f"알 수 없는 state {state}")
        problem = config.path_problem(name) or vcs.identity_problem(name, client, slot)
        if problem:
            return Outcome(name, "failed", f"슬롯이 확실하지 않아 거부: {problem}")
        has_client = vcs.client_exists(client)
        folder = os.path.isdir(slot)
        if not has_client and not folder:
            if not row:
                return Outcome(name, "skipped", "폴더·작업 공간·slots.tsv 행이 모두 없음")
            runner.rows.remove(name)
            return Outcome(name, "removed", "폴더·작업 공간은 이미 없어 slots.tsv 행만 지움")
        reasons = blockers_of(config, name, has_client)
        notes = []
        if not reasons and has_client and not discard_local:
            reasons, notes = local_change_reasons(
                config, client, False, "기록 안 된 새 파일·편집·삭제가 사라짐"
            )
        if reasons:
            return Outcome(name, "failed", "지우면 안 되는 상태", [*reasons, *notes])
        if has_client:
            vcs.delete_workspace(client)
        if folder:
            try:
                remove_tree(slot)
            except OSError as e:
                runner.rows.upsert(
                    name, path=slot.replace(os.sep, "/"), client=client, state=STATE_FAILED + "remove"
                )
                return Outcome(
                    name,
                    "failed",
                    f"폴더 삭제 실패 ({e}) — Unity 에디터·터미널 등이 잡고 있을 수 있음. 닫고 다시",
                )
        runner.rows.remove(name)
        return Outcome(name, "removed", slot, notes)


def cmd_remove(config, args):
    names = require_names(config, args.names)
    if not args.yes:
        print("거부: 되돌릴 수 없음 — 사용자 확인 후 --yes")
        return FAILED
    runner = Runner(config)
    return report([guarded(remove_one, runner, name, args.discard_local) for name in names])


def status_row(config, name, tsv):
    if not NAME_PATTERN.fullmatch(name):
        return [name, "-", "-", "-", "-", "-", "-", "-", "-", "이름 형식 오류 (slots.tsv 행)"]
    slot = config.slot_path(name)
    client = config.client_of(name)
    folder = os.path.isdir(slot)
    notes = []
    has_client, opened = "-", "-"
    if not isinstance(config.vcs, NoVcs):
        try:
            has_client = config.vcs.client_exists(client)
            opened = len(config.vcs.opened(client)) if has_client else "-"
            if has_client and folder:
                notes.append(workspace_warning(config, name))
        except RuntimeError as e:
            has_client, opened = "?", "?"
            notes.append(str(e).splitlines()[0])
        has_client = {True: "yes", False: "no"}.get(has_client, has_client)
    if name not in config.names:
        notes.append("slots.names 에 없음")
    if tsv is None and folder:
        notes.append("slots.tsv 에 없음")
    if tsv is not None and not folder:
        notes.append("폴더 없음")
    if folder and has_client == "no":
        notes.append("작업 공간 없음")
    active = active_of(config, name) if folder else None
    state, step = "-", "-"
    if active and active.id and not active.corrupt:
        fields = read_mission_fields(config.slot_state_dir(name), active.id)
        state, step = fields["상태"], fields["현재 단계"]
        notes.append(stale_note(config, name, active))
    return [
        name,
        "yes" if folder else "no",
        has_client,
        opened,
        active_text(active) if active and active.id else "-",
        state,
        step,
        (tsv or {}).get("synced", "-"),
        (tsv or {}).get("state", "-"),
        "; ".join(filter(None, notes)),
    ]


def cmd_status(config, args):
    tsv = {row["name"]: row for row in Rows(config.rows_path).read()}
    names = list(dict.fromkeys(config.check_format(name) for name in args.names))
    names = names or [*tsv, *[name for name in config.names if name not in tsv]]
    with ThreadPoolExecutor(max_workers=max(1, min(len(names), 8))) as pool:
        rows = list(pool.map(lambda name: status_row(config, name, tsv.get(name)), names))
    present = sum(1 for row in rows if row[1] == "yes")
    active = sum(1 for row in rows if row[4] != "-")
    stale = sum(1 for row in rows if "끊김 의심" in row[9])
    stale_text = f" (끊김 의심 {stale}개)" if stale else ""
    print(f"요약: 슬롯 {len(rows)}개 중 폴더 {present}개, ACTIVE {active}개{stale_text}")
    header = ["name", "folder", "client", "opened", "active", "상태", "현재 단계", "synced", "state", "비고"]
    print("\n".join(render_table(header, rows)))
    return OK


def cmd_inject(config, args):
    name = config.check_name(args.name)
    if args.file:
        try:
            with open(args.file, encoding="utf-8-sig") as stream:
                text = stream.read()
        except OSError as e:
            raise UsageError(f"--file 을 못 읽음: {e}") from e
    else:
        text = args.text or ""
    if not text.strip():
        raise UsageError("넣을 내용이 비어 있음 (--text 또는 --file)")
    session = args.session or active_of(config, name).session
    if not session:
        raise UsageError(f"슬롯 {name} 에 ACTIVE 세션이 없음 — --session 으로 줄 것")
    if not os.path.isdir(config.slot_path(name)):
        raise UsageError(f"슬롯 폴더 없음: {config.slot_path(name)}")
    try:
        return grok_inject.inject(
            config.slot_path(name),
            session,
            text,
            read_grok_leader_socket(config, name),
            log_dir=os.path.join(config.state_dir, "slots", "inject"),
            wait=args.wait,
            timeout=args.timeout,
            poll=args.poll,
            dry_run=args.dry_run,
            env=launch_env(),
        )
    except grok_inject.InjectError as e:
        print(f"실패: {e}")
        return FAILED


COMMANDS = {
    "status": cmd_status,
    "create": cmd_create,
    "refresh": cmd_refresh,
    "run": cmd_run,
    "remove": cmd_remove,
    "inject": cmd_inject,
}


def main(argv=None, runtime="claude"):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return USAGE if e.code else OK
    runtime = getattr(args, "runtime", None) or runtime
    try:
        located = locate(args.project, runtime)
        config = Config(located, read_adapter(located.adapter_path, runtime), runtime)
        return COMMANDS[args.command](config, args)
    except UsageError as e:
        print(f"사용 오류: {e}", file=sys.stderr)
        return USAGE
    except RuntimeError as e:
        print(f"실패: {e}", file=sys.stderr)
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
