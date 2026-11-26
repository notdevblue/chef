"""mission / state."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
from datetime import datetime

from chef.cli.mission_parser import OneLineParser, build_parser
from chef.cli.question_commands import (
    cmd_document_hide,
    cmd_document_list,
    cmd_document_share,
    cmd_question_answer,
    cmd_question_check,
    cmd_question_patch,
    cmd_question_preflight,
    cmd_question_preview,
    cmd_question_publish,
    cmd_question_render,
    cmd_question_restore,
    cmd_question_show,
)
from chef.guards import mission as mission_guard
from chef.infrastructure import configuration as infrastructure_configuration
from chef.infrastructure import errors as infrastructure_errors
from chef.infrastructure import locking as infrastructure_locking
from chef.infrastructure import paths as infrastructure_paths
from chef.mission.contracts import (
    ANSWER_DATE,
    APPROVED,
    ASK_FILE,
    BRIEF_FIELDS,
    CONFIRM_FILE,
    CONFIRM_KEY,
    DECISION_ID,
    DECISIONS_FILE,
    DECISIONS_HEADER,
    EDITABLE_FIELDS,
    FAILED,
    HEADER_FIELD,
    HOOKS_DIR,
    HTML_COMMENT,
    ID_PLACEHOLDER,
    ID_TEMPLATES,
    INSERTABLE_KEYS,
    LEDGER_FILE,
    LEDGER_HEADER,
    LEDGER_STATUSES,
    MISSION_STATES,
    MISSION_TIERS,
    NEEDS_HUMAN,
    NONE_CELL,
    OK,
    QUESTION_FILE,
    RUNTIMES,
    SESSION_ID,
    SHARED_DOCUMENTS_FILE,
    SHARED_DOCUMENTS_HEADER,
    SKILLS_DIR,
    SKIPPED,
    SYSTEM_DOCUMENTS,
    TEMPLATE_FILES,
    TEMPLATES_DIR,
    UNIT_HEADER,
    UNIT_ID,
    UNIT_STATUSES,
    UNITS_FILE,
    VERDICT_LIMIT,
    Context,
    StateError,
)
from chef.mission.documents import (
    auto_question_document,
    auto_question_documents,
    canonical_document_path,
    confirm_approved,
    hide_documents,
    question_inspections,
    require_question_integrity,
    share_documents,
    shared_document_rows,
    shared_documents,
    summarize_question_document,
    unanswered_questions,
)
from chef.mission.storage import (
    append_row,
    clean,
    elapsed_minutes,
    find_row,
    header_end,
    header_fields,
    header_index,
    is_overdue,
    normalize_after,
    parse_timebox,
    poc_relative_path,
    progress_of,
    ratio_of,
    read_table,
    read_text,
    require_folder,
    require_text,
    require_unit,
    stage_and_confirms,
    write_table,
    write_text,
)
from chef.mission import evidence as file_evidence
from chef.mission import unit_contracts
from chef.mission import unit_input
from chef.mission import review_dispatch
from chef.mission import review_worker
from chef.questions import records as question_records
from chef.runtimes.grok import transcript as grok_transcript
from chef.verification import execution
from chef.verification import plan as plan_policy
from chef.verification import references as reference_bundle
from chef.verification import runner as verification


def tier_messages(args, mission_id):
    tier = (getattr(args, "tier", None) or "").strip().upper()
    if not tier:
        return [
            "경고: --tier 없음. 미션은 M·L 티어용이다(S: 한두 파일 수정은 미션 없이). 다음부터 `init --tier M|L` 로 연다"
        ], ""
    if tier not in MISSION_TIERS:
        raise StateError(f"--tier 는 S·M·L 중 하나: {tier}")
    if tier == "S":
        if not getattr(args, "force_tier", False):
            raise StateError(
                f"S 티어는 미션으로 열지 않음({MISSION_TIERS['S']}). `echo han-mission close {mission_id}` 로 닫고 직접 처리할 것. "
                "사용자가 미션을 원하면 `--force-tier`"
            )
        return ["경고: S 티어를 --force-tier 로 미션으로 엶"], tier
    return [], tier


def cmd_init(ctx, args):
    active = mission_guard.read_active(ctx.state_dir)
    if active.corrupt or active.id != ctx.mission_id or not active.session:
        raise StateError(
            f"가드 안 켜짐: ACTIVE 가 미션 {ctx.mission_id} 의 세션을 가리키지 않음 — `echo han-mission open {ctx.mission_id}` 를 단독으로 먼저 실행할 것"
        )
    warnings, tier = tier_messages(args, ctx.mission_id)
    os.makedirs(ctx.folder, exist_ok=True)
    created = []
    kept = []
    for name in TEMPLATE_FILES:
        target = ctx.path(name)
        if os.path.exists(target):
            kept.append(name)
            continue
        text = read_text(os.path.join(TEMPLATES_DIR, name))
        if name in ID_TEMPLATES:
            text = text.replace(ID_PLACEHOLDER, ctx.mission_id)
        if name == "mission.md":
            text = text.replace("상태: 준비", "상태: 진행", 1)
            if tier:
                text = text.replace("현재 단계:", f"티어: {tier}\n현재 단계:", 1)
            goal = (args.goal or "").strip()
            if goal:
                if "## 목표\n" not in text:
                    raise StateError("mission.md 템플릿에 '## 목표' 제목이 없음")
                text = text.replace("## 목표\n", f"## 목표\n\n{goal}\n", 1)
        write_text(target, text)
        created.append(name)
    return [
        *warnings,
        f"init {ctx.mission_id} 만듦: {' '.join(created) or '없음'} / 건너뜀: {' '.join(kept) or '없음'}",
    ]


def cmd_mission_set(ctx, args):
    require_folder(ctx)
    if args.stage is not None or args.confirm or args.state in ("진행", "닫힘"):
        require_question_integrity(ctx.folder)
    path = ctx.path("mission.md")
    lines = read_text(path).split("\n")
    changes = []
    if args.stage is not None:
        stage = require_text("--stage", args.stage)
        known = [s.get("id") for s in ctx.adapter.get("stages") or [] if isinstance(s, dict) and s.get("id")]
        if known and stage not in known:
            raise StateError(f"--stage 허용 값: {', '.join(known)} (어댑터 stages)")
        changes.append(("현재 단계", stage))
    for item in args.confirm:
        key, separator, value = item.partition("=")
        if not separator or not CONFIRM_KEY.fullmatch(key.strip()):
            raise StateError(f"--confirm 형식은 K=V (K 는 A·B·C·A1 처럼): {item}")
        changes.append((f"확인 {key.strip()}", require_text(f"--confirm {key.strip()} 값", value)))
    if args.state:
        changes.append(("상태", args.state))
    if args.poc is not None:
        changes.append(("PoC", poc_relative_path(ctx.project, args.poc)))
    if not changes:
        raise StateError("바꿀 값 없음: --stage, --confirm K=V, --state, --poc 중 하나 이상")
    for key, value in changes:
        index = header_index(lines, key)
        if index >= 0:
            lines[index] = f"{key}: {value}"
            continue
        if not (key.startswith("확인 ") or key in INSERTABLE_KEYS):
            raise StateError(f"mission.md 헤더에 '{key}:' 줄이 없음: {path}")
        # 확인 줄이 있으면 그 마지막 뒤, 없으면 현재 단계 줄 뒤
        confirm_lines = [
            i
            for i in range(header_end(lines))
            if HEADER_FIELD.match(lines[i]) and lines[i].startswith("확인 ")
        ]
        anchor = max(confirm_lines + [header_index(lines, "현재 단계")])
        if anchor < 0:
            raise StateError(f"mission.md 헤더에 '확인 ' 줄도 '현재 단계:' 줄도 없음: {path}")
        lines.insert(anchor + 1, f"{key}: {value}")
    write_text(path, "\n".join(lines))
    fields = header_fields(lines)
    return [f"상태 {fields.get('상태', '?')} | {stage_and_confirms(fields)}"]


def cmd_unit_add(ctx, args):
    require_folder(ctx)
    path = ctx.path(UNITS_FILE)
    rows = read_table(path, UNIT_HEADER)
    if getattr(args, "input", None):
        if args.id or any(getattr(args, name) for name in BRIEF_FIELDS) or args.human != NONE_CELL or args.after != NONE_CELL:
            raise StateError("--input cannot be combined with unit id or brief flags")
        items = unit_input.load(args.input)
    else:
        items = [{"id": args.id or "", **{name: getattr(args, name) for name in BRIEF_FIELDS},
                  "human": args.human, "after": args.after}]
    additions = unit_input.prepare(items, rows)
    if not getattr(args, "dry_run", False):
        write_table(path, UNIT_HEADER, rows + additions)
    if getattr(args, "input", None) or getattr(args, "dry_run", False):
        return [json.dumps({"schema": 1, "status": "valid" if args.dry_run else "added",
                            "units": additions}, ensure_ascii=False)]
    row = additions[0]
    return [f"{row['id']} 추가 (todo, timebox {row['timebox']}분, after {row['after']})"]


def cmd_unit_template(ctx, args):
    return [json.dumps(unit_input.template(args.id), ensure_ascii=False, indent=2)]


def cmd_unit_set(ctx, args):
    require_folder(ctx)
    path = ctx.path(UNITS_FILE)
    rows = read_table(path, UNIT_HEADER)
    row = require_unit(rows, clean(args.id))
    old = dict(row)
    changed = []
    supplied = False
    for name in EDITABLE_FIELDS:
        value = getattr(args, name)
        if value is None:
            continue
        supplied = True
        cleaned = clean(value)
        if not cleaned:
            hint = f" ({NONE_CELL} 로 비움 표시)" if name in ("human", "after") else ""
            raise StateError(f"브리프 칸 {name} 을 빈 값으로 바꿀 수 없음{hint}")
        if name == "timebox":
            cleaned = parse_timebox(cleaned)
        if name == "after":
            cleaned = normalize_after(cleaned, rows, row["id"])
        if row[name] != cleaned:
            row[name] = cleaned
            changed.append(name)
    brief_changed = bool(changed)
    if args.unit is not None:
        supplied = True
        value = require_text("--unit", args.unit)
        if row["unit"] != value:
            row["unit"] = value
            changed.append(f"unit={value}")
    if changed and row["status"] == "done" and args.status != "doing":
        raise StateError("완료한 단위의 브리프를 바꾸려면 --status doing 으로 재개할 것")
    if args.status:
        supplied = True
    if args.status and args.status != row["status"]:
        if args.status == "doing":
            require_question_integrity(ctx.folder)
            pending = []
            for dependency in [d for d in row["after"].split(",") if d != NONE_CELL]:
                other = find_row(rows, "id", dependency)
                if other is not None and other["status"] != "done":
                    pending.append(f"{dependency}({other['status']})")
            if pending:
                raise StateError(f"after 가 아직 done 이 아님: {', '.join(pending)} — 먼저 끝낼 것")
            row["started"] = mission_guard.now_iso()
        if args.status == "done":
            raise StateError("완료는 complete --receipt --review --done-evidence 로 기록할 것")
        row["status"] = args.status
        changed.append(f"status={args.status}")
    if not changed:
        if supplied:
            return [f"{row['id']} 변경 없음 (no-op)"]
        raise StateError("바꿀 칸 없음: --status, --unit, --goal 같은 옵션을 줄 것")
    event = None
    with execution.unit_execution_lock(ctx.folder, row["id"]):
        if brief_changed:
            contract, event = unit_contracts.prepare_change(
                ctx.folder, old, row, mission_guard.now_iso(), read_table(ctx.path(LEDGER_FILE), LEDGER_HEADER)
            )
            unit_contracts.commit_change(ctx.folder, row, contract, lambda: write_table(path, UNIT_HEADER, rows))
        else:
            write_table(path, UNIT_HEADER, rows)
    lines = [f"{row['id']} 갱신: {', '.join(changed)}"]
    if event and event["reverificationNeeded"]:
        lines.append(json.dumps({"unit": row["id"], "contractChange": event}, ensure_ascii=False))
    return lines


def cmd_unit_list(ctx, args):
    require_folder(ctx)
    rows = read_table(ctx.path(UNITS_FILE), UNIT_HEADER)
    lines = [
        f"{row['id']} {row['status']} {progress_of(row)} {row['unit']} {row['goal']}"
        for row in rows
        if not args.status or row["status"] == args.status
    ]
    return lines or ["마일스톤 없음"]


def cmd_ledger_record(ctx, args):
    require_folder(ctx)
    require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), clean(args.unit))
    configured = (ctx.adapter.get("verify") or {}).get("grades")
    grades = [g for g in configured if isinstance(g, str)] if isinstance(configured, list) else []
    grades.append(NEEDS_HUMAN)
    grade = clean(args.grade)
    if grade not in grades:
        raise StateError(
            f"grade 허용 값: {', '.join(grades)} (어댑터 verify.grades + {NEEDS_HUMAN}): {grade}"
        )
    if getattr(args, "evidence_input", ""):
        evidence = file_evidence.capture(ctx.project, args.evidence_input, clean(args.evidence))
    else:
        evidence = require_text("--evidence", args.evidence)
    files = clean(args.files) or NONE_CELL
    append_row(
        ctx.path(LEDGER_FILE),
        LEDGER_HEADER,
        [mission_guard.now_iso(), clean(args.unit), args.status, grade, evidence, files],
    )
    return [f"ledger {clean(args.unit)} {args.status} {grade}"]


def cmd_ledger_audit(ctx, args):
    require_folder(ctx)
    result = file_evidence.audit(ctx.project, read_table(ctx.path(LEDGER_FILE), LEDGER_HEADER),
                                 args.limit, args.unit)
    args.exit_code = FAILED if result["status"] == "fail" else OK
    return [json.dumps(result, ensure_ascii=False)]


def cmd_decision_add(ctx, args):
    require_folder(ctx)
    path = ctx.path(DECISIONS_FILE)
    rows = read_table(path, DECISIONS_HEADER)
    what = require_text("--what", args.what)
    cells = [require_text(f"--{name}", getattr(args, name)) for name in ("chosen", "why", "affects")]
    fixes = clean(args.fixes or "")
    if args.fixes is not None:
        if find_row(rows, "id", fixes) is None:
            raise StateError(
                f"--fixes {fixes} 는 decisions.tsv 에 없음 (있는 것: {', '.join(r['id'] for r in rows) or '없음'})"
            )
        what = f"{fixes} 정정: {what}"
    numbers = [int(m.group(1)) for m in (DECISION_ID.fullmatch(r["id"]) for r in rows) if m]
    decision_id = f"D{max(numbers, default=0) + 1}"
    append_row(path, DECISIONS_HEADER, [mission_guard.now_iso(), decision_id, what, *cells])
    return [f"{decision_id} 추가" + (f" ({fixes} 정정)" if fixes else "")]


def active_lines(ctx, active, state):
    opened = active.opened_at or "?"
    session = active.session or "?"
    mark = (
        mission_guard.stale_text(state)
        if state.stale
        else f"살아 있음: 마지막 활동 {mission_guard.age_text(state.hours)} 전"
    )
    return [f"ACTIVE {active.id} | 세션 {session} | 열림 {opened}", mark]


def cmd_active(ctx, args):
    active = mission_guard.read_active(ctx.state_dir)
    path = mission_guard.active_path_of(ctx.state_dir)
    if active.corrupt:
        return [f"ACTIVE 손상: {path}"]
    if not active.id:
        return [f"ACTIVE 없음: {path}"]
    limit = args.hours if args.hours is not None else mission_guard.stale_hours_of(ctx.adapter)
    return [
        *active_lines(ctx, active, mission_guard.staleness(ctx.state_dir, active, limit)),
        f"파일: {path}",
    ]


def cmd_close_stale(ctx, args):
    # 끊긴(마지막 활동이 기준 시간을 넘긴) ACTIVE 만, --force-close-stale 이 있을 때만 닫음. 없으면 미리보기
    from chef.runtimes.hermes.external_slots import configured

    if configured(ctx.adapter):
        raise StateError("external slot reservation requires operator recovery")
    active = mission_guard.read_active(ctx.state_dir)
    path = mission_guard.active_path_of(ctx.state_dir)
    if not active.id:
        raise StateError(f"ACTIVE 없음: {path}")
    if active.id != ctx.mission_id:
        raise StateError(f"ACTIVE 는 {active.id} — --mission {ctx.mission_id} 와 다름")
    limit = args.hours if args.hours is not None else mission_guard.stale_hours_of(ctx.adapter)
    if not limit:
        raise StateError("끊김 기준 0(꺼짐) — --hours N 으로 기준을 줄 것")
    state = mission_guard.staleness(ctx.state_dir, active, limit)
    lines = active_lines(ctx, active, state)
    if not state.stale:
        raise StateError(
            f"아직 끊김 아님(기준 {limit:g}시간): {lines[1]} — 그 세션에서 `echo han-mission close {active.id}`"
        )
    if not args.force_close_stale:
        args.exit_code = FAILED
        return [
            *lines,
            f"미리보기: --force-close-stale 을 붙이면 ACTIVE 를 지우고 mission.md 상태를 {'유지' if args.keep_state else '중단'}으로 바꿈",
        ]
    os.remove(path)
    mission_guard.clear_allow_once(ctx.state_dir, active.id)
    done = [*lines, f"ACTIVE 지움: {path}"]
    mission_md = ctx.path("mission.md")
    if not args.keep_state and os.path.isfile(mission_md):
        text_lines = read_text(mission_md).split("\n")
        index = header_index(text_lines, "상태")
        if index >= 0 and header_fields(text_lines).get("상태") == "진행":
            text_lines[index] = "상태: 중단"
            write_text(mission_md, "\n".join(text_lines))
            done.append("mission.md 상태: 진행 → 중단")
    if os.path.isdir(ctx.folder):
        note = clean(args.reason or "") or NONE_CELL
        last = datetime.fromtimestamp(state.last).isoformat(timespec="seconds") if state.last else NONE_CELL
        with open(ctx.path("force-close.log"), "a", encoding="utf-8", newline="\n") as f:
            f.write(
                f"{mission_guard.now_iso()}\tsession={active.session or NONE_CELL}\topenedAt={active.opened_at or NONE_CELL}"
                f"\tlast={last}\tlimit={limit:g}h\treason={note}\n"
            )
        done.append(f"기록: {ctx.path('force-close.log')}")
    done.append(f"되살리기: `echo han-mission open {active.id}` 후 `mission set --state 진행`")
    return done


def cmd_status(ctx, args):
    require_folder(ctx)
    rows = read_table(ctx.path(UNITS_FILE), UNIT_HEADER)
    verdicts = read_table(ctx.path(LEDGER_FILE), LEDGER_HEADER)
    fields = header_fields(read_text(ctx.path("mission.md")).split("\n"))

    counts = {name: 0 for name in UNIT_STATUSES}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = ", ".join(f"{name} {count}" for name, count in counts.items())

    latest = {}
    for verdict in verdicts:
        latest.pop(verdict["unit"], None)
        latest[verdict["unit"]] = verdict
    recent = list(latest.values())[-VERDICT_LIMIT:]
    rest = len(latest) - len(recent)
    judged = ", ".join(f"{v['unit']} {v['status']} {v['grade']}" for v in recent) + (
        f" 외 {rest}개" if rest > 0 else ""
    )

    stuck = [f"{row['id']} timebox 넘김({ratio_of(row)})" for row in rows if is_overdue(row)]
    for name, question, issue in question_inspections(ctx.folder):
        if issue:
            stuck.append(f"{name} 기록 확인 필요")
        elif question.pending:
            stuck.append(f"{name} 답 없음")
    for row in rows:
        contract = unit_contracts.status(ctx.folder, row)
        if contract and contract["reverificationNeeded"]:
            stuck.append(f"{row['id']} reverification needed (contract v{contract['current']['version']})")
    active = mission_guard.read_active(ctx.state_dir)
    if active.id == ctx.mission_id and not active.corrupt:
        state = mission_guard.staleness(ctx.state_dir, active, mission_guard.stale_hours_of(ctx.adapter))
        if state.stale:
            stuck.append(f"ACTIVE {mission_guard.stale_text(state)}")
    return [
        f"마일스톤 {summary} | {stage_and_confirms(fields)}",
        f"판정: {judged or '없음'}",
        f"걸린 것: {', '.join(stuck) or '없음'}",
    ]


def cmd_context(ctx, args):
    lines = cmd_status(ctx, args)
    rows = read_table(ctx.path(UNITS_FILE), UNIT_HEADER)
    active = [row for row in rows if row["status"] == "doing"]
    ready = [
        row
        for row in rows
        if row["status"] == "todo"
        and all(
            require_unit(rows, dependency)["status"] == "done"
            for dependency in row["after"].split(",")
            if dependency != NONE_CELL
        )
    ]
    for row in (active + ready)[:6]:
        lines.append(
            f"{row['id']} {row['status']} {row['goal'][:160]} | 브리프: {ctx.path(UNITS_FILE)} #{row['id']}"
        )
    if len(active + ready) > 6:
        lines.append(f"다음 단위 외 {len(active + ready) - 6}개: unit list")
    decisions = read_table(ctx.path(DECISIONS_FILE), DECISIONS_HEADER)
    if getattr(args, "json", False):
        overdue = [row["id"] for row in active if is_overdue(row)]
        return [json.dumps({
            "schema": 1, "mission": ctx.mission_id,
            "status": "needs-replan" if overdue else "current",
            "active": [row["id"] for row in active], "ready": [row["id"] for row in ready],
            "overdue": overdue, "summary": lines[:3],
            "approvalSources": [ctx.path("mission.md"), *[
                str(question_records.source_path(ctx.folder, name))
                for name in question_records.document_names(ctx.folder)]],
            "unitSource": ctx.path(UNITS_FILE), "decisionSource": ctx.path(DECISIONS_FILE),
            "recentDecisions": [{"id": row["id"], "what": row["what"][:120]} for row in decisions[-4:]],
            "semanticApproval": "read_original_sources",
        }, ensure_ascii=False)]
    for row in decisions[-4:]:
        lines.append(f"{row['id']}: {row['what'][:120]} → {row['chosen'][:180]}")
    lines.append(f"승인·명세·지시 원본: {ctx.path('mission.md')} (필요한 절만 읽기)")
    lines.append(f"전체 판정·결정·질문 원본: {ctx.folder} (관련 단위·번호만 조회)")
    return lines


def verification_plan(ctx, args):
    require_folder(ctx)
    row = require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), args.id)
    plan = verification.load_plan(ctx.project, args.plan, (ctx.adapter.get("verify") or {}).get("grades", []))
    if "referenceBundle" in plan:
        try:
            reference_bundle.require_current(ctx.project, plan["referenceBundle"])
        except reference_bundle.ReferenceError as error:
            raise verification.VerificationError(str(error), "plan_invalid") from error
    plan["unitBrief"] = {name: row[name] for name in EDITABLE_FIELDS}
    contract = unit_contracts.binding(ctx.folder, row)
    if contract:
        plan["unitContract"] = contract
    return plan


def verification_guard(ctx):
    def guard(command):
        problems = []
        shell = "Bash"
        if os.name == "nt":
            shell = "PowerShell"
        verdict = mission_guard.judge_command(ctx.adapter, command, shell, problems)
        if verdict.kind != "allow" or problems:
            raise StateError(f"검증 명령 거부: {verdict.reason or problems}")

    def argv_guard(argv, cwd):
        problems = []
        verdict = mission_guard.judge_argv(ctx.adapter, argv, problems, ctx.project, cwd=cwd)
        if verdict.kind != "allow" or problems:
            raise StateError(f"검증 명령 거부: {verdict.reason or problems}")

    guard.argv = argv_guard
    return guard


def cmd_preflight(ctx, args):
    plan = verification_plan(ctx, args)
    result = verification.preflight(
        ctx.project, plan, verification_guard(ctx), settings=ctx.adapter.get("verify"), require_matches=True
    )
    args.exit_code = OK
    if result["status"] == "fail":
        args.exit_code = FAILED
    return [json.dumps(result, ensure_ascii=False)]


def cmd_schema(ctx, args):
    return [
        json.dumps(
            {
                "schema": 1,
                "stages": [
                    stage["id"]
                    for stage in ctx.adapter.get("stages", [])
                    if isinstance(stage, dict) and stage.get("id")
                ],
                "unitStatuses": UNIT_STATUSES,
                "ledgerStatuses": LEDGER_STATUSES,
                "missionStates": MISSION_STATES,
                "grades": (ctx.adapter.get("verify") or {}).get("grades", []),
                "humanGrade": NEEDS_HUMAN,
                "reviewKinds": ["poc", "unit", "integration"],
                "reviewStatuses": ["clean", "findings", "closed-low"],
                "questionItemStatuses": question_records.ITEM_STATUSES,
                "questionApprovalStatuses": question_records.APPROVAL_STATUSES,
                "runtimes": RUNTIMES,
            },
            ensure_ascii=False,
        )
    ]


def cmd_reference_snapshot(ctx, args):
    bundle = reference_bundle.snapshot(
        ctx.project, args.source, args.output, args.revision, args.section, args.expect_sha256,
        freeze=getattr(args, "freeze", False)
    )
    return [
        json.dumps(
            {"output": reference_bundle.file_pointer(ctx.project, args.output), **bundle}, ensure_ascii=False
        )
    ]


def cmd_reference_check(ctx, args):
    result = reference_bundle.check(ctx.project, args.input)
    args.exit_code = OK
    if result["status"] == "fail":
        args.exit_code = FAILED
    return [json.dumps(result, ensure_ascii=False)]


def cmd_reference_show(ctx, args):
    bundle = reference_bundle.load(ctx.project, args.input)
    return [json.dumps({**bundle, "readSources": reference_bundle.read_sources(bundle)}, ensure_ascii=False)]


def cmd_brief(ctx, args):
    require_folder(ctx)
    row = require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), args.id)
    require_question_integrity(ctx.folder)
    result = {
        "schema": 1,
        "unit": args.id,
        **{name: row[name] for name in EDITABLE_FIELDS},
        "approvalSources": [ctx.path("mission.md")],
    }
    contract = unit_contracts.status(ctx.folder, row)
    if contract:
        result["contract"] = contract
    for name in question_records.document_names(ctx.folder):
        result["approvalSources"].append(str(question_records.source_path(ctx.folder, name)))
    if args.references:
        bundle = reference_bundle.require_current(ctx.project, args.references)
        result["references"] = bundle["sources"]
        result["readSources"] = reference_bundle.read_sources(bundle)
    if is_overdue(row):
        result["dispatch"] = {
            "status": "needs-replan", "reason": "timebox_exceeded", "elapsedMinutes": elapsed_minutes(row),
            "timeboxMinutes": int(row["timebox"]),
            "next": "Return unresolved causes, conflicting references and remaining scope before reassigning this unit.",
        }
        args.exit_code = FAILED
    return [json.dumps(result, ensure_ascii=False)]


def cmd_handoff(ctx, args):
    reference_bundle.text_value(args.summary, "--summary")
    if args.max_summary_chars < 1:
        raise StateError("--max-summary-chars 는 양수여야 함")
    if len(args.summary) > args.max_summary_chars:
        raise StateError(
            f"--summary {len(args.summary)}자: 최대 {args.max_summary_chars}자; 상세를 --detail 파일로 옮기거나 --max-summary-chars 를 명시할 것"
        )
    require_folder(ctx)
    require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), args.id)
    detail = reference_bundle.file_pointer(ctx.project, args.detail)
    evidence = [reference_bundle.file_pointer(ctx.project, path) for path in args.evidence]
    return [
        json.dumps(
            {"schema": 1, "unit": args.id, "summary": args.summary, "detail": detail, "evidence": evidence},
            ensure_ascii=False,
        )
    ]


def cmd_verify(ctx, args):
    require_folder(ctx)
    require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), args.id)
    with execution.unit_execution_lock(ctx.folder, args.id):
        try:
            plan = verification_plan(ctx, args)
            if getattr(args, "coordinator", False):
                plan_policy.require_coordinator_eligible(ctx.project, plan, settings=ctx.adapter.get("verify"))
        except verification.VerificationError as error:
            if error.category == "coordinator_ineligible":
                raise
            category = error.category
            if category == "result_invalid":
                category = "plan_invalid"
            verification.record_failure(ctx.project, ctx.folder, args.id, error, category)
            raise

        path, receipt, reused = verification.run(
            ctx.project,
            ctx.folder,
            args.id,
            plan,
            verification_guard(ctx),
            args.force or getattr(args, "coordinator", False),
            settings=ctx.adapter.get("verify"),
            unit_lock_held=True,
        )
    args.exit_code = FAILED
    if receipt["status"] == "pass":
        args.exit_code = OK
    mode = "실행"
    if reused:
        mode = "재사용"
    log = f"로그: {receipt['log']}"
    if receipt["error"]:
        log += f" | {receipt['error']}"
    return [
        f"검증 {receipt['status']} {receipt['grade']} {mode} | {path}",
        log,
    ]


def cmd_review_run(ctx, args):
    require_folder(ctx)
    plan = verification_plan(ctx, args)
    scope = review_dispatch.request_array(ctx.project, args.scope)
    findings = review_dispatch.request_array(ctx.project, args.findings)
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    result = review_dispatch.run(
        ctx.project, ctx.folder, args.id, plan, ctx.adapter.get("verify") or {}, verification_guard(ctx),
        expected_snapshot=args.snapshot, purpose=args.purpose, scope=scope, findings=findings,
        argv=argv, plan_path=args.plan, timeout=args.timeout_seconds, plan_loader=lambda: verification_plan(ctx, args),
        adapter=ctx.adapter, runtime_name=ctx.runtime,
    )
    args.exit_code = FAILED if (result.get("result") or {}).get("status") == "failed" else OK
    return [json.dumps(result, ensure_ascii=False)]


def cmd_review_worker(_ctx, args):
    review_worker.run_from_file(args.input)
    return []


def cmd_review(ctx, args):
    require_folder(ctx)
    require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), args.id)
    path = verification.record_review(
        ctx.project,
        ctx.folder,
        args.id,
        args.receipt,
        args.evidence,
        args.reviewer,
        args.kind,
        args.status,
        args.findings_count,
        args.fixed_count,
        args.findings_input,
        settings=ctx.adapter.get("verify") or {},
    )
    review = verification.read_json(path)
    return [f"리뷰 {args.id} {args.status} {args.kind} | {path}", *review.get("diagnostics", [])]


def cmd_review_start(ctx, args):
    require_folder(ctx)
    require_unit(read_table(ctx.path(UNITS_FILE), UNIT_HEADER), args.id)
    plan = verification_plan(ctx, args)
    path = verification.record_review_start(ctx.project, ctx.folder, args.id, plan)
    return [f"리뷰 시작 {args.id} | {path}"]


def cmd_review_scope(ctx, args):
    plan = verification_plan(ctx, args)
    failure = ""
    try:
        paths, diff = verification.review_scope_diff(
            ctx.project, ctx.folder, args.id, plan, settings=ctx.adapter.get("verify") or {}
        )
    except verification.ScopeDiffError as error:
        paths, diff, failure = error.changed, "", str(error)
    lines = [f"리뷰 변경 범위 {args.id}: {len(paths)}개", *paths]
    if failure:
        lines.append(f"diff 쓰기 실패: {failure}")
    if diff:
        lines.append(f"리뷰 변경 diff: {diff}")
    return lines


def cmd_complete(ctx, args):
    require_folder(ctx)
    with execution.unit_execution_lock(ctx.folder, args.id):
        return complete_unit(ctx, args)


def complete_unit(ctx, args):
    require_folder(ctx)
    require_question_integrity(ctx.folder)
    rows = read_table(ctx.path(UNITS_FILE), UNIT_HEADER)
    row = require_unit(rows, args.id)
    if row["status"] not in ("doing", "done"):
        raise StateError("doing 마일스톤만 완료할 수 있음")
    require_text("--done-evidence", args.done_evidence)
    receipt = verification.read_json(args.receipt)
    verification.validate_receipt(
        ctx.project, ctx.folder, args.id, receipt, settings=ctx.adapter.get("verify") or {}
    )
    verification.validate_latest(ctx.folder, args.id, args.receipt)
    if receipt.get("plan", {}).get("unitBrief") != {name: row[name] for name in EDITABLE_FIELDS}:
        raise StateError("검증 이후 브리프·완료 조건이 바뀜")
    contract = unit_contracts.binding(ctx.folder, row)
    if contract and receipt.get("plan", {}).get("unitContract") != contract:
        raise StateError("검증 이후 unit contract version 이 바뀜; reverification needed")
    if receipt.get("grade") not in (ctx.adapter.get("verify") or {}).get("grades", []):
        raise StateError("검증 등급이 현재 어댑터와 다름")
    verification.validate_review(ctx.folder, args.id, args.receipt, args.review)
    evidence = clean(
        f"receipt={os.path.abspath(args.receipt)} review={os.path.abspath(args.review)} done={args.done_evidence}"
    )
    ledger_path = ctx.path(LEDGER_FILE)
    verdicts = read_table(ledger_path, LEDGER_HEADER)
    latest = [verdict for verdict in verdicts if verdict["unit"] == args.id]
    if not latest or latest[-1]["evidence"] != evidence:
        append_row(
            ledger_path,
            LEDGER_HEADER,
            [
                mission_guard.now_iso(),
                args.id,
                "pass",
                receipt["grade"],
                evidence,
                ",".join(receipt["snapshot"]),
            ],
        )
    row["status"] = "done"
    write_table(ctx.path(UNITS_FILE), UNIT_HEADER, rows)
    return [f"{args.id} 완료 {receipt['grade']}", *cmd_context(ctx, args)]


def search_claude(session, home, require_unique=False):
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    projects = os.path.join(base, "projects")
    if not os.path.isdir(projects):
        return ""
    found = ""
    for name in sorted(os.listdir(projects)):
        candidate = os.path.join(projects, name, f"{session}.jsonl")
        if os.path.isfile(candidate):
            if require_unique:
                if found and os.path.realpath(found) != os.path.realpath(candidate):
                    raise StateError("ambiguous Claude session transcript fallback")
                found = candidate
            else:
                return candidate
    return found


def search_codex(session, home, require_unique=False):
    base = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
    # 보관한 세션은 archived_sessions 로 옮겨져서 sessions.tsv 경로가 낡음
    found = ""
    for root in ("sessions", "archived_sessions"):
        for folder, _, names in os.walk(os.path.join(base, root)):
            for name in sorted(names):
                if name.startswith("rollout-") and name.endswith(f"-{session}.jsonl"):
                    candidate = os.path.join(folder, name)
                    if require_unique:
                        if found and os.path.realpath(found) != os.path.realpath(candidate):
                            raise StateError("ambiguous Codex session transcript fallback")
                        found = candidate
                    else:
                        return candidate
    return found


def cmd_transcripts(ctx, args):
    require_folder(ctx)
    path = ctx.path(mission_guard.SESSIONS_FILE)
    if not os.path.isfile(path):
        return [f"세션 기록 없음 ({mission_guard.SESSIONS_FILE} 은 open·resume 마커가 남김)"]
    recorded = {}
    for row in read_table(path, mission_guard.SESSIONS_HEADER):
        if not recorded.get(row["session"]):
            recorded[row["session"]] = row["transcript"]
    searches = {
        "claude": (search_claude, search_codex, grok_transcript.find_transcript),
        "codex": (search_codex, search_claude, grok_transcript.find_transcript),
        "grok": (grok_transcript.find_transcript, search_codex, search_claude),
        # Hermes owns a SQLite session store. Only an explicitly recorded
        # exported transcript is a file; never guess another runtime's log.
        "hermes": (),
    }[ctx.runtime]
    home = os.path.expanduser("~")
    lines = []
    for session, transcript in recorded.items():
        found = transcript if transcript and os.path.isfile(transcript) else ""
        # 세션 id 는 파일 이름 일치로만 찾음(와일드카드·경로 구분자가 든 값은 찾지 않음)
        if not found and SESSION_ID.fullmatch(session):
            for search in searches:
                found = found or search(session, home)
        if not found:
            lines.append(f"없음: {session}")
            continue
        lines.append(f"{session} {found}")
        if grok_transcript.is_transcript(found):
            lines.extend(f"  {child}" for child in grok_transcript.subagent_transcripts(found, home))
            continue
        subagents = os.path.join(os.path.splitext(found)[0], "subagents")
        if os.path.isdir(subagents):
            lines.extend(
                f"  {os.path.join(subagents, n)}"
                for n in sorted(os.listdir(subagents))
                if n.endswith(".jsonl")
            )
    return lines or ["세션 기록 없음"]


def cmd_inspect(ctx, args):
    if args.plan is not None:
        summary = verification.inspect_artifact(args.plan, "plan")
    else:
        summary = verification.inspect_artifact(args.receipt, "receipt")
    return [json.dumps(summary, ensure_ascii=False)]


def main(argv=None, runtime="claude"):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit:
            return OK
        if args.handler in (cmd_inspect, cmd_question_patch, cmd_review_worker, cmd_unit_template):
            for line in args.handler({}, args):
                print(line)
            return OK
        runtime = getattr(args, "runtime", None) or runtime
        try:
            located = infrastructure_configuration.locate(args.project, runtime)
            adapter = infrastructure_configuration.read_adapter(located.adapter_path, runtime)
            state_dir = mission_guard.state_dir_of(located.project, adapter)
        except (infrastructure_errors.UsageError, mission_guard.GuardOff) as e:
            raise StateError(str(e))
        mission_id = args.mission or ""
        standalone = args.handler in (
            cmd_active,
            cmd_schema,
            cmd_reference_snapshot,
            cmd_reference_show,
            cmd_reference_check,
        )
        if standalone:
            mission_id = "-"
        elif not mission_id:
            active = mission_guard.read_active(state_dir)
            if active.corrupt:
                raise StateError(
                    f"ACTIVE 손상: {mission_guard.active_path_of(state_dir)}, --mission <id> 로 지정하거나 확인 후 지울 것"
                )
            if not active.id:
                raise StateError(
                    "미션 id 없음: --mission <id> 를 주거나 `echo han-mission open <id>` 로 먼저 열 것"
                )
            mission_id = active.id
        if not standalone and not mission_guard.valid_mission_id(mission_id):
            raise StateError(f"미션 id 형식 오류: {mission_id}")
        readonly = args.handler in (
            cmd_status,
            cmd_context,
            cmd_ledger_audit,
            cmd_transcripts,
            cmd_unit_list,
            cmd_review_scope,
            cmd_document_list,
            cmd_active,
            cmd_question_preview,
            cmd_question_show,
            cmd_question_check,
            cmd_question_preflight,
            cmd_preflight,
            cmd_schema,
            cmd_brief,
            cmd_handoff,
            cmd_reference_show,
            cmd_reference_check,
        )
        readonly = readonly or (args.handler == cmd_unit_add and getattr(args, "dry_run", False))
        lock_name = mission_id
        if args.handler == cmd_verify:
            if not UNIT_ID.fullmatch(args.id):
                raise StateError("검증 단위 id 형식 오류")
            lock_name = f"{mission_id}-verify-{args.id}"
        lock = (
            contextlib.nullcontext()
            if readonly or args.handler in (cmd_verify, cmd_review_run)
            else infrastructure_locking.FileLock(
                os.path.join(state_dir, ".locks", f"{lock_name}.lock"),
                "미션 상태",
                timeout=2,
            )
        )
        with lock:
            lines = args.handler(Context(adapter, state_dir, mission_id, runtime, located.project), args)
    except execution.ExecutionBusy as error:
        print(json.dumps({"schema": 1, "status": "running", "category": error.category,
                          "execution": error.record}, ensure_ascii=False))
        return FAILED
    except (
        StateError,
        OSError,
        verification.VerificationError,
        question_records.QuestionRecordError,
        reference_bundle.ReferenceError,
        infrastructure_locking.LockBusy,
    ) as e:
        print(f"오류: {e}", file=sys.stderr)
        return FAILED
    for line in lines:
        print(line)
    return getattr(args, "exit_code", OK)


if __name__ == "__main__":
    sys.exit(main())
