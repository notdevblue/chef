"""cli / question_commands."""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from chef.cli.input_json import stdin_object
from chef.infrastructure import paths as infrastructure_paths
from chef.locations import PLUGIN_ROOT
from chef.mission import document_checks, schema_tables
from chef.mission.contracts import FAILED, QUESTION_FILE, SHARED_DOCUMENTS_FILE, StateError
from chef.mission.documents import (
    auto_question_document,
    auto_question_documents,
    canonical_document_path,
    hide_documents,
    question_inspections,
    share_documents,
    shared_documents,
)
from chef.mission.storage import require_folder
from chef.questions import records as question_records
from chef.questions import readability
from chef.verification import runner as verification


def read_input(path):
    if path != "-":
        return verification.read_json(path)
    try:
        return stdin_object()
    except ValueError as error:
        raise StateError(f"invalid JSON on standard input: {error}") from error


def cmd_question_template(_ctx, args):
    kind = "design-confirm" if args.kind == "design" else "question"
    path = PLUGIN_ROOT / "skills" / "mission" / "references" / "templates" / f"{kind}.json"
    record = read_input(path)
    if args.id:
        record["id"] = args.id
    return [json.dumps(question_records.validate_document(record), ensure_ascii=False, indent=2)]


def cmd_question_preview(ctx, args):
    record = question_records.validate_document(read_input(args.input))
    for message in readability.messages(record):
        print(message, file=sys.stderr)
    return [question_records.render_document(record).rstrip("\n")]


def cmd_question_patch(ctx, args):
    """Apply bounded changes to a draft and print JSON; never publish or execute code."""
    if args.input == args.changes == "-":
        raise StateError("patch can read only one of --input and --changes from standard input")
    record = question_records.patch_document(
        read_input(args.input),
        read_input(args.changes),
        editorial=args.editorial,
    )
    for message in readability.messages(record):
        print(message, file=sys.stderr)
    return [json.dumps(record, ensure_ascii=False, indent=2)]


def cmd_question_preflight(ctx, args):
    """Run publication and output-ownership checks without writes or a mission lock."""
    require_folder(ctx)
    record = question_records.preflight_document(
        ctx.folder,
        read_input(args.input),
        getattr(args, "expect_revision", ""),
        editorial=args.editorial,
    )
    checks = question_schema_checks(ctx, args, record)
    messages = [
        f"질문 발행 검사 통과: {record['id']}.md (revision {record['revision']})",
        *schema_messages(checks),
        *(f"답변 부담 확인: {note}" for note in question_records.decision_review_notes(record)),
        *readability.messages(record),
    ]
    if getattr(args, "preview", False):
        for message in messages:
            print(message, file=sys.stderr)
        return [question_records.render_document(record).rstrip("\n")]
    return messages


def cmd_question_publish(ctx, args):
    require_folder(ctx)
    draft = question_records.validate_document(read_input(args.input))
    checks = question_schema_checks(ctx, args, draft)
    record = question_records.publish_document(
        ctx.folder,
        draft,
        getattr(args, "expect_revision", ""),
        editorial=args.editorial,
    )
    return [
        f"질문 저장: {record['id']}.md (revision {record['revision']})",
        *schema_messages(checks),
        *(f"답변 부담 확인: {note}" for note in question_records.decision_review_notes(record)),
        *readability.messages(record),
    ]


def question_schema_checks(ctx, args, record):
    name = f"{record['id']}.md"
    settings = ctx.adapter.get("documents", {})
    require_typed = bool(isinstance(settings, dict) and settings.get("requireSchemaTables"))
    require_typed = require_typed or (
        getattr(args, "unified", False) and bool(re.fullmatch(r"confirm-A[0-9]*", record["id"]))
    )
    previous = None
    if require_typed and question_records.source_path(ctx.folder, name).is_file():
        previous = question_records.read_document(ctx.folder, name)
    checks = schema_tables.check_blocks(record, ctx.project, ctx.adapter, previous, require_typed=require_typed)
    if require_typed and not record.get("detail_blocks"):
        detail = f"details/{name}"
        _, path = canonical_document_path(ctx.folder, detail)
        if os.path.isfile(path):
            result = document_checks.check(detail, path, Path(path).read_bytes(), ctx.folder, ctx.project, ctx.adapter)
            if result["issues"]:
                raise StateError(f"{detail}: {'; '.join(result['issues'])}")
            checks.extend(result["schemaChecks"])
    return checks


def schema_messages(checks):
    for item in checks:
        reason = f" ({item['reason']})" if "reason" in item else ""
        yield f"Schema {item['kind']} {item['table']}: {item['status']}{reason}"


def cmd_question_answer(ctx, args):
    require_folder(ctx)
    record = question_records.answer_document(ctx.folder, args.name, read_input(args.input))
    return [f"답변 저장: {record['id']}.md (revision {record['revision']})"]


def cmd_question_restore(ctx, args):
    require_folder(ctx)
    record = question_records.restore_document(
        ctx.folder, args.input, args.source, args.expect_revision, args.expect_sha256
    )
    return [f"질문 기록 복구: {record['id']}.md (revision {record['revision']})"]


def cmd_question_show(ctx, args):
    require_folder(ctx)
    return [json.dumps(question_records.read_document(ctx.folder, args.name), ensure_ascii=False, indent=2)]


def cmd_question_render(ctx, args):
    require_folder(ctx)
    record = question_records.render_saved_document(ctx.folder, args.name)
    return [f"질문 문서 생성: {record['id']}.md (revision {record['revision']})"]


def cmd_question_check(ctx, args):
    require_folder(ctx)
    inspected = question_inspections(ctx.folder)
    issues = []
    for name, _, issue in inspected:
        if not issue:
            continue
        if name == "questions":
            issues.append(f"questions 기록 확인 필요: {issue}; questions 폴더와 JSON 파일 이름을 확인할 것")
        else:
            source = os.path.join(ctx.folder, "questions", f"{name[:-3]}.json")
            issues.append(
                f"{name} 기록 확인 필요: {issue}; 정본 {source} 확인 후 question publish --input <JSONfile>, "
                f"생성본 복구는 question render {name}"
            )
    if issues:
        args.exit_code = FAILED
        return issues
    return [f"질문 기록 정상: {len(inspected)}개"]


def cmd_document_compare(_ctx, args):
    from chef.questions.check import check

    result = check(args.path, args.source, args.archive)
    args.exit_code = FAILED if result["issues"] else 0
    return [json.dumps(result, ensure_ascii=False, indent=2)]


def cmd_document_share(ctx, args):
    require_folder(ctx)
    checked = []
    as_json = getattr(args, "json", False)

    def validate(name, path, content):
        result = inspect_content(ctx, name, path, content)
        if result["issues"]:
            raise StateError(f"{name}: {'; '.join(result['issues'])}")
        checked.append(result)

    names = share_documents(ctx.folder, args.paths,
                            check_content=validate if as_json or getattr(args, "unified", False) else None)
    if as_json:
        return [json.dumps({"shared": names, "documents": checked}, ensure_ascii=False)]
    return [f"공유함: {', '.join(names)}",
            *(message for item in checked for message in schema_messages(item["schemaChecks"]))]


def inspect_content(ctx, name, path, content):
    parts = name.split("/")
    if QUESTION_FILE.fullmatch(parts[-1]) and auto_question_document(ctx.folder, name):
        result = {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "checked": "question",
                  "issues": [], "schemaChecks": [], "approval": None}
        try:
            question = parts[-1]
            root_text = (content.decode("utf-8-sig") if len(parts) == 1
                         else (Path(ctx.folder) / question).read_text(encoding="utf-8-sig"))
            record = question_records.checked_document(ctx.folder, question, root_text)
            if parts[0] == "details" and len(parts) == 2 and not record.get("detail_blocks"):
                result = document_checks.check(name, path, content, ctx.folder, ctx.project, ctx.adapter)
            result["approval"] = record["approval"]["status"]
            result["pendingItems"] = [item["id"] for item in record["items"] if item["status"] == "pending"]
            if parts[:2] != ["details", "history"]:
                result["schemaChecks"].extend(schema_tables.check_blocks(record, ctx.project, ctx.adapter))
        except (ValueError, OSError) as error:
            result["issues"].append(str(error))
        return result
    return document_checks.check(name, path, content, ctx.folder, ctx.project, ctx.adapter)


def cmd_document_create(ctx, args):
    require_folder(ctx)
    if not document_checks.SCHEMA_NAME.fullmatch(args.name) or QUESTION_FILE.fullmatch(args.name + ".md"):
        raise StateError("invalid or reserved document name")
    prefix = "" if args.kind == "report" else "details/"
    name, target = canonical_document_path(ctx.folder, prefix + args.name + ".md")
    if args.kind == "schema":
        if not args.input or args.title:
            raise StateError("schema creation needs --input and derives its title from the block")
        block = read_input(args.input)
        text = document_checks.schema_text(block)
        checks = [schema_tables.check_block(block, ctx.project, ctx.adapter)]
    else:
        if not args.title or args.input:
            raise StateError("report and detail drafts require a title")
        text = document_checks.draft(args.kind, args.title)
        checks = []
    path = Path(target)
    if os.path.normcase(path.relative_to(infrastructure_paths.request_realpath(ctx.folder)).as_posix()) != os.path.normcase(name):
        raise StateError("document creation cannot follow aliases")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except FileExistsError as error:
        raise StateError(f"{name} already exists. Edit it in place or choose a different --name.") from error
    return [f"문서 생성: {name}", *schema_messages(checks)]


def document_results(ctx, paths, shared):
    results = []
    for raw in paths:
        name, path = canonical_document_path(ctx.folder, raw)
        result = inspect_content(ctx, name, path, Path(path).read_bytes())
        result["sharing"] = (
            "automatic" if auto_question_document(ctx.folder, name) else "shared" if name in shared else "unshared"
        )
        result.setdefault("approval", None)
        results.append(result)
    return results


def cmd_document_check(ctx, args):
    require_folder(ctx)
    results = document_results(ctx, args.paths, shared_documents(ctx.folder))
    if any(result["issues"] for result in results):
        args.exit_code = FAILED
    return [json.dumps(results, ensure_ascii=False)]


def cmd_document_hide(ctx, args):
    require_folder(ctx)
    names = hide_documents(ctx.folder, args.paths)
    automatic = sorted(
        {
            canonical_document_path(ctx.folder, name)[0]
            for name in args.paths
            if auto_question_document(ctx.folder, name)
        }
    )
    hidden = sorted(set(names) - set(automatic))
    result = [f"숨김: {', '.join(hidden) or '변경 없음'}"]
    if automatic:
        result.append(f"자동 표시 유지: {', '.join(automatic)}")
    return result


def cmd_document_list(ctx, args):
    require_folder(ctx)
    automatic = sorted(auto_question_documents(ctx.folder))
    result = []
    manifest = ctx.path(SHARED_DOCUMENTS_FILE)
    if os.path.lexists(manifest) and (
        not infrastructure_paths.inside_root(
            infrastructure_paths.request_realpath(ctx.folder), infrastructure_paths.request_realpath(manifest)
        )
        or not os.path.isfile(manifest)
    ):
        raise StateError(f"{SHARED_DOCUMENTS_FILE} 경로가 잘못됨: {manifest}")
    try:
        names = sorted(shared_documents(ctx.folder))
        if args.json:
            paths = sorted(set(names) | set(automatic))
            return [json.dumps(document_results(ctx, paths, set(names)), ensure_ascii=False)]
        result.append(f"공유 중: {', '.join(names) or '없음'}")
    except (OSError, UnicodeError, StateError) as error:
        result.append(f"읽기 실패: {SHARED_DOCUMENTS_FILE} ({error})")
    if automatic:
        result.append(f"자동 표시: {', '.join(automatic)}")
    return result
