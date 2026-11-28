"""cli / mission_parser."""

import argparse
from pathlib import Path

from chef.mission.contracts import (
    BRIEF_FIELDS,
    EDITABLE_FIELDS,
    LEDGER_STATUSES,
    MISSION_STATES,
    NONE_CELL,
    RUNTIMES,
    UNIT_STATUSES,
    StateError,
)


class OneLineParser(argparse.ArgumentParser):
    # 사용법 전문 대신 한 줄 오류로 끝냄(하위 파서도 이 클래스를 물려받음)
    def error(self, message):
        raise StateError(f"{self.prog}: {message} (-h 로 사용법 확인)")


def build_parser(handlers=None, *, prog="mission_state.py", unified=False):
    if handlers is None:
        from chef.mission import state as handlers

    parser = OneLineParser(prog=prog, description="미션 상태 파일 쓰기")
    parser.add_argument("--project", help="어댑터를 찾기 시작할 폴더 (기본: 현재 폴더)")
    parser.add_argument("--mission", help="미션 id (기본: <stateDir>/ACTIVE 의 id)")
    parser.add_argument(
        "--runtime", choices=RUNTIMES, default=None, help="어댑터 검색. 기본은 호출 런타임, Grok은 grok"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--mission", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--runtime", choices=RUNTIMES, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    def leaf(parent, name, handler, **kwargs):
        child = parent.add_parser(name, parents=[common], **kwargs)
        child.set_defaults(handler=handler)
        return child

    init = leaf(sub, "init", handlers.cmd_init)
    init.add_argument("--goal")
    init.add_argument("--tier", help="S|M|L. S 는 --force-tier 없이는 거부, 없으면 경고")
    init.add_argument("--force-tier", action="store_true", help="S 티어도 미션으로 연다(사용자가 원할 때만)")
    leaf(sub, "status", handlers.cmd_status)
    context = leaf(sub, "context", handlers.cmd_context)
    context.add_argument("--json", action="store_true", help="compact current-state pointers for a resumed coordinator")
    leaf(sub, "schema", handlers.cmd_schema)
    inspect = leaf(sub, "inspect", handlers.cmd_inspect)
    source = inspect.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", default=None)
    source.add_argument("--receipt", default=None)
    brief = leaf(sub, "brief", handlers.cmd_brief)
    brief.add_argument("id")
    brief.add_argument("--references", default="")
    handoff = leaf(sub, "handoff", handlers.cmd_handoff)
    handoff.add_argument("id")
    handoff.add_argument("--summary", required=True)
    handoff.add_argument("--detail", required=True)
    handoff.add_argument("--evidence", action="append", default=[])
    handoff.add_argument("--max-summary-chars", type=int, default=1000)
    reference = sub.add_parser("reference").add_subparsers(dest="action", required=True)
    snapshot = leaf(reference, "snapshot", handlers.cmd_reference_snapshot)
    snapshot.add_argument("--source", action="append", required=True)
    snapshot.add_argument("--output", required=True)
    snapshot.add_argument("--revision", default="")
    snapshot.add_argument("--section", default="")
    snapshot.add_argument("--expect-sha256", default="")
    snapshot.add_argument("--freeze", action="store_true", help="source 내용을 content-addressed 사본으로 고정")
    leaf(reference, "check", handlers.cmd_reference_check).add_argument("--input", required=True)
    leaf(reference, "show", handlers.cmd_reference_show).add_argument("--input", required=True)
    leaf(sub, "transcripts", handlers.cmd_transcripts)
    leaf(sub, "active", handlers.cmd_active).add_argument(
        "--hours", type=float, help="끊김 기준 시간 (기본: 어댑터 mission.staleHours, 24)"
    )
    close_stale = leaf(sub, "close-stale", handlers.cmd_close_stale)
    close_stale.add_argument("--force-close-stale", action="store_true", help="실제로 닫음. 없으면 미리보기")
    close_stale.add_argument(
        "--hours", type=float, help="끊김 기준 시간 (기본: 어댑터 mission.staleHours, 24)"
    )
    close_stale.add_argument("--keep-state", action="store_true", help="mission.md 상태를 진행으로 둠")
    close_stale.add_argument("--reason", default="")

    question = sub.add_parser("question").add_subparsers(dest="action", required=True)
    template = leaf(question, "template", handlers.cmd_question_template)
    template.add_argument("--kind", choices=("question", "design"), required=True)
    template.add_argument("--id", help="replace the sample question id")
    preview = leaf(question, "preview", handlers.cmd_question_preview)
    preview.add_argument("--input", required=True, help="draft JSON file, or - for standard input")
    patch = leaf(question, "patch", handlers.cmd_question_patch)
    patch.description = "제한된 draft 변경 JSON을 적용해 stdout JSON 출력. 정본·이력·revision 자동 변경 없음."
    patch.add_argument("--input", required=True, help="draft JSON file, or - for standard input")
    patch.add_argument(
        "--changes", required=True, help="bounded changes JSON file, or - for standard input"
    )
    patch.add_argument(
        "--editorial", action="store_true", help="기존 답·선택·승인 범위를 보존하는 문체 patch"
    )
    question_preflight = leaf(question, "preflight", handlers.cmd_question_preflight)
    question_preflight.description = "publish와 같은 개정·승인·답 보존·상세 소유 검사를 읽기 전용 실행."
    question_preflight.add_argument("--input", required=True, help="draft JSON file, or - for standard input")
    question_preflight.add_argument("--expect-revision", type=int, default=argparse.SUPPRESS)
    question_preflight.add_argument("--editorial", action="store_true")
    question_preflight.add_argument("--preview", action="store_true", help="render the checked draft to stdout")
    publish = leaf(question, "publish", handlers.cmd_question_publish)
    publish.add_argument("--input", required=True, help="draft JSON file, or - for standard input")
    publish.add_argument("--expect-revision", type=int, default=argparse.SUPPRESS)
    publish.add_argument("--editorial", action="store_true")
    answer = leaf(question, "answer", handlers.cmd_question_answer)
    answer.add_argument("name")
    answer.add_argument("--input", required=True, help="reply JSON file, or - for standard input")
    reply_template = leaf(question, "reply-template", handlers.cmd_question_reply_template)
    reply_template.description = "Print a blank reply with the current revision, timestamp and a new id."
    reply_template.add_argument("name")
    restore = leaf(question, "restore", handlers.cmd_question_restore)
    restore.add_argument("--input", required=True)
    restore.add_argument("--source", required=True)
    restore.add_argument("--expect-revision", type=int, required=True)
    restore.add_argument("--expect-sha256", required=True)
    leaf(question, "show", handlers.cmd_question_show).add_argument("name")
    leaf(question, "render", handlers.cmd_question_render).add_argument("name")
    leaf(question, "check", handlers.cmd_question_check)

    document = sub.add_parser("document").add_subparsers(dest="action", required=True)
    compare = leaf(document, "compare", handlers.cmd_document_compare)
    compare.description = "Compare a draft with its source and archive without opening a mission."
    compare.add_argument("path", type=Path)
    compare.add_argument("--source", type=Path)
    compare.add_argument("--archive", type=Path)
    create_document = leaf(document, "create", handlers.cmd_document_create)
    create_document.add_argument("--kind", choices=("report", "detail", "schema"), required=True)
    create_document.add_argument("--name", default="report")
    create_document.add_argument("--title")
    create_document.add_argument("--input", help="schema-table JSON file or stdin(-)")
    leaf(document, "check", handlers.cmd_document_check).add_argument("paths", nargs="+")
    share = leaf(document, "share", handlers.cmd_document_share)
    share.add_argument("paths", nargs="+")
    share.add_argument("--json", action="store_true", help="return the shared byte hashes and check results")
    leaf(document, "hide", handlers.cmd_document_hide).add_argument("paths", nargs="+")
    leaf(document, "list", handlers.cmd_document_list).add_argument("--json", action="store_true")

    plan = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    for action, handler in (("template", handlers.cmd_plan_template), ("show", handlers.cmd_plan_show),
                            ("save", handlers.cmd_plan_save)):
        command = leaf(plan, action, handler)
        command.add_argument("id")
        if action == "template":
            command.add_argument("--grade")
        elif action == "save":
            command.add_argument("--input", required=True, help="plan JSON file or stdin (-)")
            command.add_argument("--expect-sha256")

    verify = leaf(sub, "verify", handlers.cmd_verify)
    verify.add_argument("id")
    verify.add_argument("--plan", required=not unified, help="defaults to the registered unit plan in chef")
    verify.add_argument("--force", action="store_true")
    verify.add_argument("--coordinator", action="store_true", help="명시적으로 허용된 순수 검사만 코디네이터에서 실행")
    preflight = leaf(sub, "preflight", handlers.cmd_preflight)
    preflight.add_argument("id")
    preflight.add_argument("--plan", required=not unified, help="defaults to the registered unit plan in chef")
    worker = leaf(sub, "_review-worker", handlers.cmd_review_worker)
    worker.add_argument("--input", required=True)
    review_run = leaf(sub, "review-run", handlers.cmd_review_run)
    review_run.add_argument("id")
    review_run.add_argument("--plan", required=not unified, help="defaults to the registered unit plan in chef")
    for field in ("snapshot", "purpose", "scope", "findings"):
        review_run.add_argument(f"--{field}", required=True)
    review_run.add_argument("--timeout-seconds", type=int, default=600)
    review_run.add_argument("argv", nargs="+")
    review = leaf(sub, "review", handlers.cmd_review, allow_abbrev=False)
    review.add_argument("id")
    review.add_argument("--job", help="record a bound structured worker result by job key")
    review.add_argument("--session", help="current coordinator session ID for --job")
    for field in ("receipt", "evidence", "reviewer"):
        review.add_argument(f"--{field}")
    review.add_argument("--kind", choices=("poc", "unit", "integration"))
    review.add_argument("--status", choices=("clean", "findings", "closed-low"))
    review.add_argument("--findings-count", type=int, default=-1, help="남은 지적 수. 생략하면 미기록")
    review.add_argument("--fixed-count", type=int, default=-1, help="직전 지적 중 이번에 해소를 확인한 수")
    review.add_argument(
        "--findings-input", default="", help="지적 id·kind·origin(·severity)·resolvedIds JSON 파일 또는 stdin(-)"
    )
    review.add_argument("--resolved-id", action="append", default=[], help="diff에서 해소를 확인한 id (closed-low)")
    leaf(sub, "review-open", handlers.cmd_review_open).add_argument("id")
    start = leaf(sub, "review-start", handlers.cmd_review_start)
    start.add_argument("id")
    start.add_argument("--plan", required=not unified, help="defaults to the registered unit plan in chef")
    scope = leaf(sub, "review-scope", handlers.cmd_review_scope)
    scope.add_argument("id")
    scope.add_argument("--plan", required=not unified, help="defaults to the registered unit plan in chef")
    complete = leaf(sub, "complete", handlers.cmd_complete)
    complete.add_argument("id")
    for field in ("receipt", "review", "done-evidence"):
        complete.add_argument(f"--{field}", required=True)

    mission = sub.add_parser("mission").add_subparsers(dest="action", required=True)
    mission_set = leaf(mission, "set", handlers.cmd_mission_set)
    mission_set.add_argument("--stage")
    mission_set.add_argument("--confirm", action="append", default=[], metavar="K=V")
    mission_set.add_argument("--state", choices=MISSION_STATES)
    mission_set.add_argument("--poc", metavar="PATH")

    unit = sub.add_parser("unit").add_subparsers(dest="action", required=True)
    template = leaf(unit, "template", handlers.cmd_unit_template)
    template.add_argument("--id", action="append", default=[], help="template unit id; repeat for a batch")
    add = leaf(unit, "add", handlers.cmd_unit_add)
    add.add_argument("id", nargs="?")
    add.add_argument("--input", help="schema 1 units JSON file or standard input (-); validates the entire batch")
    add.add_argument("--dry-run", action="store_true", help="validate without writing mission state")
    for name in BRIEF_FIELDS:
        add.add_argument(f"--{name}", default="")
    add.add_argument("--human", default=NONE_CELL)
    add.add_argument("--after", default=NONE_CELL)
    unit_set = leaf(unit, "set", handlers.cmd_unit_set)
    unit_set.add_argument("id")
    unit_set.add_argument("--status", choices=UNIT_STATUSES)
    unit_set.add_argument("--unit")
    for name in EDITABLE_FIELDS:
        unit_set.add_argument(f"--{name}")
    leaf(unit, "list", handlers.cmd_unit_list).add_argument("--status", choices=UNIT_STATUSES)

    ledger = sub.add_parser("ledger").add_subparsers(dest="action", required=True)
    record = leaf(ledger, "record", handlers.cmd_ledger_record)
    record.add_argument("unit")
    record.add_argument("--status", required=True, choices=LEDGER_STATUSES)
    record.add_argument("--grade", required=True)
    record.add_argument("--evidence", default="")
    record.add_argument("--files", default="")
    record.add_argument("--evidence-input", default="", help="schema 1 evidence JSON file or standard input (-)")
    audit = leaf(ledger, "audit", handlers.cmd_ledger_audit)
    audit.add_argument("--unit", default="")
    audit.add_argument("--limit", type=int, default=20)

    decision = sub.add_parser("decision").add_subparsers(dest="action", required=True)
    decide = leaf(decision, "add", handlers.cmd_decision_add)
    for name in ("what", "chosen", "why", "affects"):
        decide.add_argument(f"--{name}", default="")
    decide.add_argument("--fixes")
    return parser
