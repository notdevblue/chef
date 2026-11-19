"""cli / question_commands."""

import json
import os

from chef.infrastructure import paths as infrastructure_paths
from chef.mission.contracts import FAILED, SHARED_DOCUMENTS_FILE, StateError
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
from chef.verification import runner as verification


def cmd_question_preview(ctx, args):
    record = question_records.validate_document(verification.read_json(args.input))
    return [question_records.render_document(record).rstrip("\n")]


def cmd_question_patch(ctx, args):
    """Apply bounded changes to a draft and print JSON; never publish or execute code."""
    record = question_records.patch_document(
        verification.read_json(args.input),
        verification.read_json(args.changes),
        editorial=args.editorial,
    )
    return [json.dumps(record, ensure_ascii=False, indent=2)]


def cmd_question_preflight(ctx, args):
    """Run publication and output-ownership checks without writes or a mission lock."""
    require_folder(ctx)
    record = question_records.preflight_document(
        ctx.folder,
        verification.read_json(args.input),
        getattr(args, "expect_revision", ""),
        editorial=args.editorial,
    )
    return [
        f"질문 발행 검사 통과: {record['id']}.md (revision {record['revision']})",
        *(f"답변 부담 확인: {note}" for note in question_records.decision_review_notes(record)),
    ]


def cmd_question_publish(ctx, args):
    require_folder(ctx)
    record = question_records.publish_document(
        ctx.folder,
        verification.read_json(args.input),
        getattr(args, "expect_revision", ""),
        editorial=args.editorial,
    )
    return [
        f"질문 저장: {record['id']}.md (revision {record['revision']})",
        *(f"답변 부담 확인: {note}" for note in question_records.decision_review_notes(record)),
    ]


def cmd_question_answer(ctx, args):
    require_folder(ctx)
    record = question_records.answer_document(ctx.folder, args.name, verification.read_json(args.input))
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


def cmd_document_share(ctx, args):
    require_folder(ctx)
    names = share_documents(ctx.folder, args.paths)
    return [f"공유함: {', '.join(names)}"]


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
        result.append(f"공유 중: {', '.join(names) or '없음'}")
    except (OSError, UnicodeError, StateError) as error:
        result.append(f"읽기 실패: {SHARED_DOCUMENTS_FILE} ({error})")
    if automatic:
        result.append(f"자동 표시: {', '.join(automatic)}")
    return result
