"""Question records, explicit answer history, rendering and guarded publication."""

import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from chef.infrastructure.storage import atomic_write_bytes
from chef.locations import PLUGIN_ROOT
from chef.questions.blocks import (
    QuestionRecordError,
    option_prose,
    render_blocks,
    render_options,
    render_selection,
    validate_blocks,
    validate_options,
)
from chef.questions.summary import QuestionItem, QuestionSummary, markdown_lines
from chef.questions.authored import approval_from_answer, render_choice, validate_authored

DOCUMENT_ID = re.compile(r"(?:ask|confirm)-[A-Za-z0-9][A-Za-z0-9_.-]*")
RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ITEM_STATUSES = ("pending", "answered", "withdrawn")
APPROVAL_STATUSES = ("none", "pending", "complete")
STATUS_LABELS = {"pending": "답변 대기", "answered": "답변 완료", "withdrawn": "철회"}
APPROVAL_LABELS = {"none": "없음", "pending": "승인 대기", "complete": "승인 완료"}
DOCUMENT_FIELDS = ("schema", "id", "revision", "title", "intro", "items", "body", "approval", "answers")
ITEM_FIELDS = ("id", "title", "body", "status", "answer")
UPDATE_FIELDS = ("id", "status", "answer")
ANSWER_FIELDS = ("schema", "id", "revision", "at", "text", "updates")
CHOICE_LEAD = re.compile(r"^\s*(?:[-*+]\s+)?\**(?:권장|대안)(?:\s+[\w-]+)?\**\s*[:：·—]")
SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])([ \t]+)")
MARKDOWN_LINK = re.compile(r"!?\[(?:\\.|[^\]])*\]\((?:\\.|[^)])*\)")
HTML_TAG = re.compile(r"</?[^>\n]+?>")
MARKDOWN_BLOCK_START = re.compile(
    r"(?:#{1,6}\s|[-+*]\s+|\d+[.)]\s+|>|`{3,}|~{3,}|(?:-{3,}|_{3,}|\*{3,})\s*$)"
)


def validate_identifier(value, label, pattern=RECORD_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value) or ".." in value or value.endswith("."):
        raise QuestionRecordError(f"{label} 형식 오류: {value}")
    return value


def document_id(name):
    if not isinstance(name, str) or not name.endswith(".md"):
        raise QuestionRecordError(f"질문 문서는 루트 Markdown 이름이어야 함: {name}")
    return validate_identifier(name[:-3], "문서 id", DOCUMENT_ID)


def contained_path(folder, *parts):
    root = Path(folder).resolve()
    path = root.joinpath(*parts)
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise QuestionRecordError(f"미션 밖 질문 경로: {path}")
        if any(part.startswith(".") for part in resolved.relative_to(root).parts):
            raise QuestionRecordError(f"숨김 질문 경로: {path}")
    except (OSError, RuntimeError) as error:
        raise QuestionRecordError(f"질문 경로 확인 실패: {path}: {error}") from error
    return path


def source_path(folder, name):
    identifier = document_id(name)
    return contained_path(folder, "questions", f"{identifier}.json")


def document_names(folder):
    directory = contained_path(folder, "questions")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise QuestionRecordError(f"질문 저장소가 폴더가 아님: {directory}")
    names = []
    for path in sorted(directory.glob("*.json")):
        name = f"{path.stem}.md"
        source_path(folder, name)
        names.append(name)
    return names


def required_text(value, field, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise QuestionRecordError(f"{field} 문자열이 필요함")
    return value


def validate_fields(value, required, optional=(), label="기록"):
    if not isinstance(value, dict):
        raise QuestionRecordError(f"{label} 객체가 필요함")
    missing = set(required) - set(value)
    unknown = set(value) - set(required) - set(optional)
    if missing:
        raise QuestionRecordError(f"{label} 필수 필드 없음: {', '.join(sorted(missing))}")
    if unknown:
        raise QuestionRecordError(
            f"{label} 알 수 없는 필드: {', '.join(sorted(str(field) for field in unknown))}"
        )


def validate_revision(value):
    if type(value) is not int or value < 1:
        raise QuestionRecordError("revision은 양의 정수여야 함")
    return value


def validate_approval(value):
    validate_fields(value, ("status", "reason"), label="approval")
    if value.get("status") not in APPROVAL_STATUSES:
        raise QuestionRecordError("approval.status 형식 오류")
    required_text(value.get("reason"), "approval.reason", value["status"] == "pending")


def validate_item(value, label="문항"):
    if not isinstance(value, dict):
        raise QuestionRecordError(f"{label} 객체가 필요함")
    validate_identifier(value.get("id"), f"{label} id")
    if value.get("status") not in ITEM_STATUSES:
        raise QuestionRecordError(f"{label} status 형식 오류")
    required_text(value.get("answer"), f"{label} answer", value["status"] != "pending")


def validate_answer(value):
    validate_fields(value, ANSWER_FIELDS, ("approval",), "답 기록")
    if type(value.get("schema")) is not int or value["schema"] != 1:
        raise QuestionRecordError("답 기록 schema는 1이어야 함")
    validate_identifier(value.get("id"), "답 기록 id")
    validate_revision(value.get("revision"))
    at = required_text(value.get("at"), "답 기록 at", True)
    try:
        datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError as error:
        raise QuestionRecordError("답 기록 at은 ISO 날짜/시각이어야 함") from error
    required_text(value.get("text"), "답 원문", True)
    updates = value.get("updates")
    if not isinstance(updates, list):
        raise QuestionRecordError("답 기록 updates 배열이 필요함")
    identifiers = set()
    for update in updates:
        validate_fields(update, UPDATE_FIELDS, ("selected",), label="답 갱신")
        validate_item(update, "답 갱신")
        if "selected" in update:
            validate_identifier(update["selected"], "selected")
            if update["status"] != "answered":
                raise QuestionRecordError("selected requires answered status")
        if update["id"] in identifiers:
            raise QuestionRecordError(f"답 갱신 id 중복: {update['id']}")
        identifiers.add(update["id"])
    if "approval" in value:
        validate_approval(value["approval"])
    return value


def validate_document(record):
    validate_fields(record, DOCUMENT_FIELDS, ("legacy_markdown", "blocks", "detail_blocks"), "질문")
    if type(record.get("schema")) is not int or record["schema"] not in (1, 2):
        raise QuestionRecordError("질문 schema는 1 또는 2이어야 함")
    if record["schema"] == 2:
        validate_authored(record)
    validate_identifier(record.get("id"), "문서 id", DOCUMENT_ID)
    validate_revision(record.get("revision"))
    required_text(record.get("title"), "title", True)
    required_text(record.get("intro"), "intro")
    required_text(record.get("body"), "body")
    if record["schema"] == 1:
        validate_blocks(record)
    validate_approval(record.get("approval"))
    items = record.get("items")
    if not isinstance(items, list) or not items:
        raise QuestionRecordError("items는 비어 있지 않은 배열이어야 함")
    identifiers = set()
    for item in items:
        validate_fields(item, ITEM_FIELDS, ("heading", "options", "selected"), "문항")
        validate_item(item)
        validate_options(item)
        required_text(item.get("title"), "문항 title", True)
        required_text(item.get("body"), "문항 body")
        if "heading" in item:
            heading = required_text(item["heading"], "문항 heading", True)
            if heading.splitlines() != [heading]:
                raise QuestionRecordError("문항 heading은 한 줄이어야 함")
        if item["id"] in identifiers:
            raise QuestionRecordError(f"문항 id 중복: {item['id']}")
        identifiers.add(item["id"])
    answers = record.get("answers")
    if not isinstance(answers, list):
        raise QuestionRecordError("answers 배열이 필요함")
    operations = set()
    for answer in answers:
        validate_answer(answer)
        if answer["id"] in operations:
            raise QuestionRecordError(f"답 기록 id 중복: {answer['id']}")
        if answer["revision"] > record["revision"]:
            raise QuestionRecordError("답 기록 revision이 문서 revision보다 큼")
        if answer["revision"] == record["revision"]:
            for update in answer["updates"]:
                if update["id"] not in identifiers:
                    raise QuestionRecordError(f"현재 개정 답 대상 문항이 없음: {update['id']}")
                if "selected" in update:
                    item = next(item for item in items if item["id"] == update["id"])
                    if update["selected"] not in {option["id"] for option in item.get("options", [])}:
                        raise QuestionRecordError("answer selected must name an existing option id")
        operations.add(answer["id"])
    if "legacy_markdown" in record:
        required_text(record["legacy_markdown"], "legacy_markdown")
    return copy.deepcopy(record)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise QuestionRecordError(f"질문 JSON 읽기 실패: {path}: {error}") from error


def read_document(folder, name):
    record = validate_document(read_json(source_path(folder, name)))
    if record["id"] != document_id(name):
        raise QuestionRecordError(f"질문 문서 id와 파일 이름 불일치: {name}")
    return record


def summarize_document(record):
    record = validate_document(record)
    items = []
    for item in record["items"]:
        status = "answered"
        if item["status"] == "pending":
            status = "pending"
        items.append(QuestionItem(item["id"], item["title"], status))
    pending = any(item.status == "pending" for item in items) or record["approval"]["status"] == "pending"
    return QuestionSummary(f"{record['id']}.md", record["title"], items, pending, True, True)


def item_heading(item):
    if "heading" in item:
        return item["heading"]
    title = item["title"]
    if title.startswith(f"{item['id']}.") or title == item["id"]:
        return title
    return f"{item['id']}. {title}"


def unmatched_options(item):
    body_lines = {line.strip() for line in markdown_lines(item["body"])}
    return [
        line
        for option in item.get("options", [])
        if (line := f"- {option_prose(option)}") not in body_lines and line[2:] not in body_lines
    ]


def _protected_inline_spans(text):
    spans = [match.span() for pattern in (MARKDOWN_LINK, HTML_TAG) for match in pattern.finditer(text)]

    def escaped(position):
        slashes = 0
        position -= 1
        while position >= 0 and text[position] == "\\":
            slashes += 1
            position -= 1
        return slashes % 2 == 1

    index = 0
    while index < len(text):
        if text[index] != "`" or escaped(index):
            index += 1
            continue
        end = index + 1
        while end < len(text) and text[end] == "`":
            end += 1
        delimiter = text[index:end]
        closing = -1
        cursor = end
        while cursor < len(text):
            candidate = text.find("`", cursor)
            if candidate < 0:
                break
            run_end = candidate + 1
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            run = text[candidate:run_end]
            if run == delimiter:
                closing = run_end
                break
            cursor = run_end
        if closing < 0:
            index = end
            continue
        spans.append((index, closing))
        index = closing
    return sorted(spans)


def _format_legacy_choice_prose(value):
    """Add Markdown hard breaks at safe sentence boundaries without editing prose."""
    if len(value) <= 120:
        return value
    spans = _protected_inline_spans(value)
    breaks = []
    span_index = 0
    for match in SENTENCE_BREAK.finditer(value):
        start, end = match.span(1)
        while span_index < len(spans) and spans[span_index][1] <= start:
            span_index += 1
        if span_index < len(spans) and spans[span_index][0] < end:
            continue
        punctuation = value[start - 1]
        following = value[end] if end < len(value) else ""
        if punctuation == "." and start >= 2 and value[start - 2].isdigit():
            continue
        line_end = value.find("\n", end)
        if line_end < 0:
            line_end = len(value)
        if MARKDOWN_BLOCK_START.match(value[end:line_end].lstrip()):
            continue
        breaks.append((start, end))
    for start, end in reversed(breaks):
        value = value[:start] + "  \n" + value[end:]
    return value


def render_legacy_choice(option):
    """Render schema 1 option fields as separate Markdown prose."""
    lines = [f"- {escape(option['label'], quote=False)}"]
    for key, label in (("description", "설명"), ("impact", "영향")):
        if option.get(key):
            prose = _format_legacy_choice_prose(option[key]).replace("\n", "\n  ")
            lines.append(f"  {label} · {escape(prose, quote=False)}")
    return "  \n".join(lines)


def decision_review_notes(record):
    """Flag review workload and overlapping choice prose without changing approval status."""
    pending = [item for item in record["items"] if item["status"] == "pending"]
    notes = []
    if len(pending) > 5:
        notes.append(
            f"미답 {len(pending)}문항: 이번에 답해야 할 선행 결정부터 묻고, "
            "함께 승인해야 하는 이유와 표·묶음 승인 안의 개별 판단 수를 확인하세요."
        )
    for item in pending:
        if item.get("options") and any(CHOICE_LEAD.match(line) for line in markdown_lines(item["body"])):
            notes.append(
                f"문항 {item['id']}: 본문과 options에 선택지가 있습니다. "
                "선택과 영향은 한 번만 쓰고 본문에는 판단에 필요한 맥락을 남기세요."
            )
    return notes


def _render_document(record, *, legacy_layout=False, tabular_choices=False, legacy_prose_choices=False):
    record = validate_document(record)
    old_choices = legacy_layout or tabular_choices
    number_items = all(not item["id"].isdecimal() and "heading" not in item for item in record["items"])
    summary = summarize_document(record)
    approval = record["approval"]
    status = "답변 완료"
    if summary.pending:
        status = "답변 대기"
    sections = [
        f"# {record['title']}",
        f"문서: `{record['id']}` · 개정: {record['revision']}",
        f"상태: {status} · 미답 {summary.pending_count}/{len(summary.items)}",
        f"별도 승인 요청: {APPROVAL_LABELS[approval['status']]}",
    ]
    if approval["reason"]:
        if record["schema"] == 2 and approval_from_answer(record):
            sections.append(f"[현재 승인 조건 원문](details/history/{record['id']}.md)")
        else:
            sections.append(approval["reason"])
    if record["intro"]:
        sections.append(record["intro"])
    if summary.pending_count or legacy_layout:
        sections.append("## 결정할 것")
    for number, item in enumerate(record["items"], 1):
        if item["status"] != "pending":
            continue
        heading = item_heading(item)
        if not old_choices and number_items and "options" in item:
            heading = f"{number}. {item['title']}"
        sections.append(f"### {heading}")
        if item["body"]:
            sections.append(item["body"])
        if old_choices and "options" in item:
            sections.append(render_options(item["options"]))
        elif record["schema"] == 2 and "options" in item:
            sections.extend(render_choice(option) for option in item["options"])
        elif "options" in item and (choices := unmatched_options(item)):
            if legacy_prose_choices:
                sections.append("\n".join(choices))
            else:
                unmatched = set(choices)
                sections.extend(
                    render_legacy_choice(option)
                    for option in item["options"]
                    if f"- {option_prose(option)}" in unmatched
                )
        if old_choices or "options" not in item:
            sections.append(f"- 상태: {STATUS_LABELS[item['status']]}")
        if item["answer"] and record["schema"] == 1:
            sections.extend(["답 내용:", item["answer"]])
    selected = [item for item in record["items"] if "selected" in item]
    if selected:
        sections.append("## 현재 선택")
        if record["schema"] == 2:
            for item in selected:
                option = next(option for option in item["options"] if option["id"] == item["selected"])
                sections.extend(["### " + item["title"], render_choice(option)])
        elif old_choices:
            sections.extend(render_selection(item) for item in selected)
        elif legacy_prose_choices:
            sections.append(
                "\n".join(
                    f"- {item['title']} · "
                    + option_prose(next(option for option in item["options"] if option["id"] == item["selected"]))
                    for item in selected
                )
            )
        else:
            for item in selected:
                option = next(option for option in item["options"] if option["id"] == item["selected"])
                sections.extend(["### " + item["title"], render_legacy_choice(option)])
    if "blocks" in record:
        sections.append(render_blocks(record["blocks"], record["schema"]))
    elif record["body"]:
        sections.append(record["body"])
    if record.get("detail_blocks"):
        sections.append(f"[상세](details/{record['id']}.md)")
    archive_target = f"details/history/{record['id']}.md"
    if legacy_layout or f"]({archive_target})" not in "\n\n".join(sections):
        sections.extend(["## 전체 이력", f"[질문·답 원문 전체 이력]({archive_target})"])
    return "\n\n".join(sections) + "\n"


def render_document(record, *, legacy_layout=False, tabular_choices=False):
    return _render_document(record, legacy_layout=legacy_layout, tabular_choices=tabular_choices)


def history_path(folder, name):
    return contained_path(folder, "details", "history", f"{document_id(name)}.md")


def render_record_history(record, include_evidence=True):
    sections = [
        f"## 개정 {record['revision']}",
        f"### {record['title']}",
        record["intro"],
        f"별도 승인 요청: {APPROVAL_LABELS[record['approval']['status']]}",
        record["approval"]["reason"],
    ]
    for item in record["items"]:
        heading = item_heading(item)
        sections.extend(
            [
                f"#### {heading}",
                f"문항: `{item['id']}` · {item['title']}",
                item["body"],
                f"- 상태: {STATUS_LABELS[item['status']]}",
                item["answer"],
            ]
        )
        if "options" in item:
            sections.append(render_options(item["options"]))
        if "selected" in item:
            sections.append(render_selection(item))
    sections.append(record["body"])
    if "blocks" in record:
        sections.append(render_blocks(record["blocks"], record["schema"]))
    if record.get("detail_blocks"):
        sections.extend(["### 상세", render_blocks(record["detail_blocks"], record["schema"])])
    if include_evidence:
        sections.append(render_history_evidence(record["answers"], [record.get("legacy_markdown", "")]))
    return "\n\n".join(sections) + "\n"


def render_history_evidence(answers, legacy_documents):
    sections = ["## 답 원문 이력"]
    for answer in answers:
        sections.extend(
            [f"### {answer['id']} · {answer['at']} · 대상 개정 {answer['revision']}", answer["text"]]
        )
        for update in answer["updates"]:
            sections.extend([f"- 문항 {update['id']}: {STATUS_LABELS[update['status']]}", update["answer"]])
            if "selected" in update:
                sections.append(f"선택 id: `{update['selected']}`")
        if "approval" in answer:
            sections.extend(
                [
                    f"별도 승인 요청: {APPROVAL_LABELS[answer['approval']['status']]}",
                    answer["approval"]["reason"],
                ]
            )
    for number, legacy in enumerate(legacy_documents, 1):
        if not legacy:
            continue
        fence = "`" * max(3, max((len(value) + 1 for value in re.findall(r"`+", legacy)), default=3))
        sections.extend([f"## 이전 Markdown 원문 {number}", f"{fence}markdown\n{legacy}\n{fence}"])
    return "\n\n".join(sections) + "\n"


def history_records(folder, record, additions=()):
    directory = contained_path(folder, "questions", "history", record["id"])
    records = []
    for path in sorted(directory.glob("*.json")):
        snapshot = validate_document(
            read_json(contained_path(folder, *path.relative_to(Path(folder).resolve()).parts))
        )
        if (
            snapshot["id"] != record["id"]
            or path.name != f"{snapshot['revision']}.json"
            or snapshot["revision"] >= record["revision"]
        ):
            raise QuestionRecordError(f"질문 개정 이력 불일치: {path}")
        records.append(snapshot)
    for path in sorted(directory.glob("recovery/*/receipt.json")):
        receipt = read_json(contained_path(folder, *path.relative_to(Path(folder).resolve()).parts))
        if not isinstance(receipt, dict):
            raise QuestionRecordError(f"질문 복구 이력 영수증 형식 오류: {path}")
        if receipt.get("status") != "complete":
            continue
        revision = validate_revision(receipt.get("revision"))
        if (
            receipt.get("id") != record["id"]
            or revision > record["revision"]
            or not isinstance(receipt.get("sha256"), dict)
        ):
            raise QuestionRecordError(f"질문 복구 이력 불일치: {path}")
        for filename in ("before.json", "after.json"):
            snapshot_path = contained_path(
                folder, *path.parent.relative_to(Path(folder).resolve()).parts, filename
            )
            data = snapshot_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != receipt.get("sha256", {}).get(filename):
                raise QuestionRecordError(f"질문 복구 이력 해시 불일치: {snapshot_path}")
            snapshot = validate_document(read_json(snapshot_path))
            if snapshot["id"] != record["id"] or snapshot["revision"] != receipt["revision"]:
                raise QuestionRecordError(f"질문 복구 이력 개정 불일치: {snapshot_path}")
            records.append(snapshot)
    unique = []
    for snapshot in [*records, *additions, record]:
        if snapshot not in unique:
            unique.append(snapshot)
    unique.sort(key=lambda snapshot: snapshot["revision"])
    return unique


def render_history_document(folder, record, additions=()):
    record = validate_document(record)
    sections = [
        f"# {record['title']} — 전체 이력",
        f"문서: `{record['id']}`",
        f"[현재 문서](../../{record['id']}.md)",
    ]
    answers = []
    legacy_documents = []
    for snapshot in history_records(folder, record, additions):
        sections.append(render_record_history(snapshot, False))
        answer_ids = []
        for answer in snapshot["answers"]:
            if answer not in answers:
                answers.append(answer)
            answer_ids.append(f"`{answer['id']}` (대상 개정 {answer['revision']})")
        if answer_ids:
            sections.append("이 개정의 답 기록: " + ", ".join(answer_ids))
        if "legacy_markdown" in snapshot:
            legacy = snapshot["legacy_markdown"]
            if legacy not in legacy_documents:
                legacy_documents.append(legacy)
            sections.append(f"이 개정의 이전 Markdown 원문: {legacy_documents.index(legacy) + 1}")
    sections.append(render_history_evidence(answers, legacy_documents))
    return "\n\n".join(sections) + "\n"


def validate_history_archive(archive_path, source_record):
    archive_path = Path(archive_path)
    folder = archive_path.parent.parent.parent
    name = f"{source_record['id']}.md"
    expected_path = history_path(folder, name)
    if archive_path.absolute() != expected_path.absolute():
        raise QuestionRecordError("전체 이력은 details/history/<id>.md 경로여야 함")
    current = read_document(folder, name)
    if source_record not in history_records(folder, current):
        raise QuestionRecordError("원문 문서와 개정이 전체 이력 정본에 없음")
    if expected_path.read_bytes() != render_history_document(folder, current).encode("utf-8"):
        raise QuestionRecordError("전체 이력이 질문 정본과 다르거나 변조됨")
    return True


def normalize_markdown(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def matches_current_render(record, text):
    if not isinstance(text, str):
        return False
    text = normalize_markdown(text)
    return (
        text == normalize_markdown(render_document(record))
        or text == normalize_markdown(_render_document(record, legacy_prose_choices=True))
        or text == normalize_markdown(render_document(record, tabular_choices=True))
        or text == normalize_markdown(render_document(record, legacy_layout=True))
    )


def checked_document(folder, name, text_snapshot):
    record = read_document(folder, name)
    contained_path(folder, name)
    if not matches_current_render(record, text_snapshot):
        raise QuestionRecordError(f"생성 Markdown과 질문 JSON 불일치: {name}")
    validate_history_archive(history_path(folder, name), record)
    if record.get("detail_blocks"):
        detail = contained_path(folder, "details", name)
        if detail.read_bytes() != render_detail_document(record).encode("utf-8"):
            raise QuestionRecordError(f"생성 상세와 질문 JSON 불일치: {name}")
    return record


def inspect_document(folder, name, text_snapshot):
    try:
        return summarize_document(checked_document(folder, name, text_snapshot)), ""
    except (QuestionRecordError, OSError) as error:
        return QuestionSummary(name, name, [], False, False, False), str(error)


def file_stamp(path):
    try:
        status = os.stat(path)
    except OSError:
        return 0, -1
    return status.st_mtime_ns, status.st_size


def inspection_inputs(folder, name):
    identifier = document_id(name)
    folder = os.path.abspath(folder)
    directory = Path(folder, "questions", "history", identifier)
    inputs = [
        os.path.join(folder, name),
        os.path.join(folder, "questions", f"{identifier}.json"),
        os.path.join(folder, "details", "history", f"{identifier}.md"),
        *directory.glob("*.json"),
    ]
    detail = os.path.join(folder, "details", name)
    if os.path.exists(detail):
        inputs.append(detail)
    for receipt in directory.glob("recovery/*/receipt.json"):
        inputs.extend([receipt, receipt.with_name("before.json"), receipt.with_name("after.json")])
    return sorted({os.fspath(path) for path in inputs})


def inspection_stamp(folder, name, text_snapshot):
    required_text(text_snapshot, "Markdown 본문")
    stamps = tuple(
        (path, os.path.realpath(path), *file_stamp(path)) for path in inspection_inputs(folder, name)
    )
    text_hash = hashlib.sha256(text_snapshot.encode("utf-8", errors="surrogatepass")).hexdigest()
    newest = max((stamp[2] for stamp in stamps), default=0)
    return (os.path.abspath(folder), os.path.realpath(folder), name, text_hash, stamps), newest


def write_text(path, text):
    write_bytes(path, text.encode("utf-8"))


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_bytes(path, value):
    atomic_write_bytes(path, value)


def replace_expected_bytes(
    folder, parts, expected, replacement, expected_exists=True, replacement_exists=True
):
    path = contained_path(folder, *parts)
    if path.exists() != expected_exists or (expected_exists and path.read_bytes() != expected):
        raise QuestionRecordError(f"복구 저장 전 현재 파일이 바뀜: {path}")
    if replacement_exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(contained_path(folder, *parts), replacement)
    else:
        contained_path(folder, *parts).unlink()


def validate_restoration(current, record, evidence):
    if record["id"] != current["id"] or record["revision"] != current["revision"]:
        raise QuestionRecordError("복구 id와 revision은 현재 정본과 같아야 함")
    if record == current:
        raise QuestionRecordError("복구 입력이 현재 정본과 같음; 새 복구 없이 question check로 확인할 것")
    if record["answers"][: len(current["answers"])] != current["answers"]:
        raise QuestionRecordError("복구는 기존 answers를 정확한 앞부분으로 보존해야 함")
    if ("legacy_markdown" in record) != ("legacy_markdown" in current) or record.get(
        "legacy_markdown"
    ) != current.get("legacy_markdown"):
        raise QuestionRecordError("복구는 legacy_markdown 존재와 원문을 보존해야 함")
    current_items = {item["id"]: item for item in current["items"]}
    items = {item["id"]: item for item in record["items"]}
    if not set(current_items).issubset(items):
        raise QuestionRecordError("복구는 기존 문항 id를 모두 보존해야 함")
    outcomes = {
        identifier: {field: item.get(field) for field in ("status", "answer", "selected")}
        for identifier, item in current_items.items()
    }
    approval = current["approval"]
    evidence = normalize_markdown(evidence).strip()
    for answer in record["answers"][len(current["answers"]) :]:
        if normalize_markdown(answer["text"]).strip() not in evidence:
            raise QuestionRecordError(f"복구 답 원문이 Markdown 근거에 없음: {answer['id']}")
        for update in answer["updates"]:
            if update["id"] not in items:
                raise QuestionRecordError(f"복구 답 대상 문항이 없음: {update['id']}")
            outcomes[update["id"]] = {field: update.get(field) for field in ("status", "answer", "selected")}
        if "approval" in answer:
            approval = answer["approval"]
    for identifier, item in items.items():
        expected = outcomes.get(identifier, {"status": "pending", "answer": ""})
        if any(item.get(field) != expected.get(field) for field in ("status", "answer", "selected")):
            raise QuestionRecordError(f"복구 문항 상태와 답에 대응하는 추가 이력이 필요함: {identifier}")
    if record["approval"] != approval:
        raise QuestionRecordError("복구 승인 변경에 대응하는 추가 이력이 필요함")


def restore_document(folder, input_path, source, expected_revision, expected_sha256):
    try:
        reviewed_bytes = Path(input_path).read_bytes()
        record = validate_document(json.loads(reviewed_bytes.decode("utf-8-sig")))
        evidence_bytes = Path(source).read_bytes()
        evidence = evidence_bytes.decode("utf-8-sig")
    except (OSError, ValueError) as error:
        raise QuestionRecordError(f"복구 입력 읽기 실패: {error}") from error
    name = f"{record['id']}.md"
    path = source_path(folder, name)
    markdown_path = contained_path(folder, name)
    try:
        original_json = path.read_bytes()
        original_markdown = markdown_path.read_bytes()
        current = validate_document(json.loads(original_json.decode("utf-8-sig")))
    except (OSError, ValueError) as error:
        raise QuestionRecordError(f"복구에는 유효한 기존 JSON과 Markdown이 필요함: {error}") from error
    if type(expected_revision) is not int or current["revision"] != expected_revision:
        raise QuestionRecordError("복구 expected_revision이 현재 revision과 다름")
    if (
        not isinstance(expected_sha256, str)
        or hashlib.sha256(original_json).hexdigest() != expected_sha256.lower()
    ):
        raise QuestionRecordError("복구 expected_sha256이 현재 JSON과 다름")
    if evidence_bytes != original_markdown:
        raise QuestionRecordError("복구 source가 현재 Markdown 바이트와 다름")
    validate_restoration(current, record, evidence)
    restored_json = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    restored_markdown = render_document(record).encode("utf-8")
    archive = history_path(folder, name)
    archive_existed = archive.exists()
    original_archive = b""
    if archive_existed:
        original_archive = archive.read_bytes()
    restored_archive = render_history_document(folder, record, (current,)).encode("utf-8")
    detail = detail_output(folder, record, current)
    original_detail = b""
    detail_existed = False
    if detail is not None:
        detail_path = contained_path(folder, *detail[0])
        detail_existed = detail_path.exists()
        if detail_existed:
            original_detail = detail_path.read_bytes()
    repaired_at = datetime.now(timezone.utc)
    history = contained_path(
        folder,
        "questions",
        "history",
        record["id"],
        "recovery",
        f"{repaired_at.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}",
    )
    snapshots = {
        "before.json": original_json,
        "before.md": original_markdown,
        "input.json": reviewed_bytes,
        "after.json": restored_json,
        "after.md": restored_markdown,
        "before-history.md": original_archive,
        "after-history.md": restored_archive,
    }
    if detail is not None:
        snapshots.update({"before-detail.md": original_detail, "after-detail.md": detail[1] or b""})
    receipt = {
        "schema": 1,
        "id": record["id"],
        "revision": record["revision"],
        "repaired_at": repaired_at.isoformat(),
        "status": "prepared",
        "before_history_existed": archive_existed,
        "sha256": {key: hashlib.sha256(value).hexdigest() for key, value in snapshots.items()},
    }
    if detail is not None:
        receipt["before_detail_existed"] = detail_existed
        receipt["after_detail_existed"] = detail[1] is not None
    try:
        history.mkdir(parents=True, exist_ok=False)
        for filename, value in snapshots.items():
            write_bytes(
                contained_path(folder, *history.relative_to(Path(folder).resolve()).parts, filename), value
            )
        write_json(history / "receipt.json", receipt)
        if path.read_bytes() != original_json or markdown_path.read_bytes() != original_markdown:
            raise QuestionRecordError("복구 준비 중 현재 JSON 또는 Markdown이 바뀜")
        if archive.exists() != archive_existed or (
            archive_existed and archive.read_bytes() != original_archive
        ):
            raise QuestionRecordError("복구 준비 중 현재 전체 이력이 바뀜")
        if detail is not None:
            detail_path = contained_path(folder, *detail[0])
            if detail_path.exists() != detail_existed or (
                detail_existed and detail_path.read_bytes() != original_detail
            ):
                raise QuestionRecordError("복구 준비 중 현재 상세가 바뀜")
    except OSError as error:
        raise QuestionRecordError(f"복구 백업 저장 실패: {history}: {error}") from error
    live_targets = [
        (("questions", f"{record['id']}.json"), original_json, restored_json, True, True),
        ((name,), original_markdown, restored_markdown, True, True),
        (("details", "history", name), original_archive, restored_archive, archive_existed, True),
    ]
    if detail is not None:
        live_targets.append(
            (detail[0], original_detail, detail[1] or b"", detail_existed, detail[1] is not None)
        )
    try:
        for parts, original, restored, existed, after_exists in live_targets:
            if existed or after_exists:
                replace_expected_bytes(folder, parts, original, restored, existed, after_exists)
        if any(
            contained_path(folder, *parts).exists() != after_exists
            or (after_exists and contained_path(folder, *parts).read_bytes() != restored)
            for parts, _, restored, _, after_exists in live_targets
        ):
            raise QuestionRecordError("복구 저장 중 현재 JSON, Markdown 또는 전체 이력이 바뀜")
        receipt["status"] = "complete"
        write_json(history / "receipt.json", receipt)
    except (OSError, QuestionRecordError) as error:
        rollback_errors = []
        for parts, original, restored, existed, after_exists in live_targets:
            try:
                live = contained_path(folder, *parts)
                actual = b""
                if live.exists():
                    actual = live.read_bytes()
                if live.exists() == after_exists and actual == restored:
                    if existed or after_exists:
                        replace_expected_bytes(folder, parts, restored, original, after_exists, existed)
                elif live.exists() != existed or actual != original:
                    raise QuestionRecordError(f"외부 변경을 덮어쓸 수 없음: {live}")
            except (OSError, QuestionRecordError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        receipt["status"] = "rolled_back"
        if rollback_errors:
            receipt["status"] = "rollback_failed"
        try:
            write_json(history / "receipt.json", receipt)
        except OSError as receipt_error:
            rollback_errors.append(str(receipt_error))
        detail = "; 원본 복원됨"
        if rollback_errors:
            detail = f"; 롤백 확인 필요: {'; '.join(rollback_errors)}"
        raise QuestionRecordError(f"질문 복구 저장 실패: {error}; 백업: {history}{detail}") from error
    return record


def render_detail_document(record):
    return (
        f"<!-- chef:question-detail:{record['id']}:v1 -->\n\n# {record['title']} — 상세\n\n"
        f"[현재 문서](../{record['id']}.md)\n\n" + render_blocks(record["detail_blocks"], record["schema"]) + "\n"
    )


def detail_output(folder, record, current):
    parts = ("details", f"{record['id']}.md")
    path = contained_path(folder, *parts)
    generated = bool(record.get("detail_blocks"))
    previously_generated = current is not None and bool(current.get("detail_blocks"))
    if not generated and not previously_generated:
        return None
    if path.exists():
        if not previously_generated or path.read_bytes() != render_detail_document(current).encode("utf-8"):
            raise QuestionRecordError(f"수동 상세 파일 보호: {path}; 기존 생성 소유와 정확한 내용이 필요함")
    if generated:
        return parts, render_detail_document(record).encode("utf-8")
    return parts, None


def prepare_outputs(folder, record, current, snapshots=(), preserve_source=False):
    name = f"{record['id']}.md"
    outputs = list(snapshots)
    outputs.extend(
        [
            (
                ("questions", f"{record['id']}.json"),
                (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            ),
            ((name,), render_document(record).encode("utf-8")),
            (
                ("details", "history", name),
                render_history_document(
                    folder,
                    record,
                    (current,) if current and current["revision"] < record["revision"] else (),
                ).encode("utf-8"),
            ),
        ]
    )
    detail = detail_output(folder, record, current)
    if detail is not None:
        outputs.append(detail)
    plan = []
    resolved = set()
    for parts, replacement in outputs:
        path = contained_path(folder, *parts)
        if path.resolve() in resolved:
            raise QuestionRecordError(f"출력 경로 충돌: {path}")
        resolved.add(path.resolve())
        existed = path.exists()
        original = path.read_bytes() if existed else None
        if parts == ("details", name) and original is not None:
            if (
                current is None
                or not current.get("detail_blocks")
                or original != render_detail_document(current).encode("utf-8")
            ):
                raise QuestionRecordError(f"수동 상세 파일 보호: {path}; 준비 중 상세가 바뀜")
        if parts == ("questions", f"{record['id']}.json"):
            if current is None and existed:
                raise QuestionRecordError("질문 저장 준비 중 JSON이 생성됨")
            if current is not None:
                try:
                    actual = json.loads(original.decode("utf-8-sig")) if original is not None else None
                except (UnicodeError, ValueError) as error:
                    raise QuestionRecordError("질문 저장 준비 중 JSON이 바뀜") from error
                if actual != current:
                    raise QuestionRecordError("질문 저장 준비 중 JSON이 바뀜")
            if preserve_source:
                replacement = original
        plan.append((parts, original, replacement))
    return plan


def commit_outputs(folder, plan):
    """CAS each output and undo only this transaction's bytes on a write failure."""
    attempted = []
    try:
        for parts, original, replacement in plan:
            path = contained_path(folder, *parts)
            actual = path.read_bytes() if path.exists() else None
            if actual != original:
                raise QuestionRecordError(f"질문 저장 전 파일이 바뀜: {path}")
            if actual == replacement:
                continue
            attempted.append((parts, original, replacement))
            if replacement is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_text(contained_path(folder, *parts), replacement.decode("utf-8"))
        for parts, _, replacement in plan:
            path = contained_path(folder, *parts)
            if (path.read_bytes() if path.exists() else None) != replacement:
                raise QuestionRecordError(f"질문 저장 중 파일이 바뀜: {path}")
    except (OSError, QuestionRecordError) as error:
        rollback_errors = []
        for parts, original, replacement in reversed(attempted):
            try:
                path = contained_path(folder, *parts)
                actual = path.read_bytes() if path.exists() else None
                if actual == original:
                    continue
                if actual != replacement:
                    raise QuestionRecordError(f"외부 변경을 덮어쓸 수 없음: {path}")
                if original is None:
                    path.unlink()
                else:
                    write_bytes(path, original)
            except (OSError, QuestionRecordError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = "; 원본 복원됨"
        if rollback_errors:
            detail = "; 롤백 확인 필요: " + "; ".join(rollback_errors)
        raise QuestionRecordError(f"질문 JSON/Markdown 저장 실패: {error}{detail}") from error


def render_saved_document(folder, name):
    record = read_document(folder, name)
    plan = prepare_outputs(folder, record, record, preserve_source=True)
    commit_outputs(folder, plan)
    return record


def validate_publication(current, record, editorial):
    if current["schema"] == 2 and record["schema"] != 2:
        raise QuestionRecordError("schema 2 문서는 schema 1로 되돌릴 수 없음")
    current_items = {item["id"]: item for item in current["items"]}
    items = {item["id"]: item for item in record["items"]}
    if editorial:
        for field in ("blocks", "detail_blocks"):
            before = [
                {key: value for key, value in block.items() if key != "title"}
                for block in current.get(field, [])
                if block["type"] in ("table", "fields", "literal")
            ]
            after = [
                {key: value for key, value in block.items() if key != "title"}
                for block in record.get(field, [])
                if block["type"] in ("table", "fields", "literal")
            ]
            if before != after:
                raise QuestionRecordError(f"editorial 발행은 {field} table/fields 계약을 변경할 수 없음")
        if record.get("legacy_markdown", "") != current.get("legacy_markdown", "") or (
            "legacy_markdown" in record
        ) != ("legacy_markdown" in current):
            raise QuestionRecordError("editorial 발행은 legacy_markdown을 변경할 수 없음")
        if set(current_items) != set(items):
            raise QuestionRecordError("editorial 발행은 문항 id 집합을 변경할 수 없음")
        for identifier, item in items.items():
            previous = current_items[identifier]
            if item["status"] != previous["status"] or item["answer"] != previous["answer"]:
                raise QuestionRecordError("editorial 발행은 문항 상태나 답 내용을 변경할 수 없음")
            if item.get("options") != previous.get("options") or item.get("selected") != previous.get(
                "selected"
            ):
                raise QuestionRecordError("editorial 발행은 options나 selected를 변경할 수 없음")
        if (
            record["approval"]["status"] != current["approval"]["status"]
            or record["answers"] != current["answers"]
        ):
            raise QuestionRecordError("editorial 발행은 승인이나 답 이력을 변경할 수 없음")
        return
    if record["approval"] != current["approval"] and record["approval"]["status"] != "pending":
        raise QuestionRecordError("승인 대기 해소는 answer로 기록해야 함")
    for identifier, item in items.items():
        if identifier not in current_items:
            if item["status"] == "answered":
                raise QuestionRecordError(f"새 문항은 answered로 발행할 수 없음: {identifier}")
            continue
        previous = current_items[identifier]
        definition_changed = any(
            item.get(field, "") != previous.get(field, "")
            for field in ("title", "body", "heading", "options")
        )
        if definition_changed:
            if item["status"] != "pending" or item["answer"] != "" or "selected" in item:
                raise QuestionRecordError(
                    f"내용이 바뀐 문항은 pending과 빈 answer로 다시 열어야 함: {identifier}"
                )
            continue
        answer_changed = (
            item["status"] != previous["status"]
            or item["answer"] != previous["answer"]
            or item.get("selected") != previous.get("selected")
        )
        if answer_changed and (item["status"] != "pending" or item["answer"] != ""):
            raise QuestionRecordError(f"기존 문항 답 변경은 answer로 기록해야 함: {identifier}")
    scope_changed = any(
        record.get(field) != current.get(field) for field in ("intro", "body", "blocks", "detail_blocks")
    )
    if scope_changed and any(item["status"] != "pending" for item in record["items"]):
        if record["approval"]["status"] != "pending":
            raise QuestionRecordError("전체 범위가 바뀌면 해결된 문항을 유지하기 전에 approval 대기가 필요함")


def publication_plan(folder, record, expected_revision="", editorial=False):
    """Prepare all publish checks and bytes without creating files or directories."""
    if type(editorial) is not bool:
        raise QuestionRecordError("editorial은 bool이어야 함")
    record = validate_document(record)
    name = f"{record['id']}.md"
    path = source_path(folder, name)
    markdown_path = contained_path(folder, name)
    current = None
    snapshots = []
    try:
        if path.exists():
            current = read_document(folder, name)
            if record == current:
                return record, prepare_outputs(folder, record, current, preserve_source=True)
            if type(expected_revision) is not int or expected_revision != current["revision"]:
                raise QuestionRecordError("질문 expected_revision이 현재 revision과 다름")
            if record["revision"] <= current["revision"]:
                raise QuestionRecordError("변경한 질문 revision은 현재보다 커야 함")
            if record["answers"] != current["answers"]:
                raise QuestionRecordError("답 원문 이력 변경은 answer로 기록해야 함")
            if "legacy_markdown" in current and record.get("legacy_markdown") != current["legacy_markdown"]:
                raise QuestionRecordError("기존 legacy_markdown을 삭제하거나 변경할 수 없음")
            validate_publication(current, record, editorial)
            history = contained_path(
                folder, "questions", "history", record["id"], f"{current['revision']}.json"
            )
            if history.exists():
                if read_json(history) != current:
                    raise QuestionRecordError(f"질문 개정 이력 충돌: {history}")
            else:
                snapshots.append(
                    (
                        ("questions", "history", record["id"], f"{current['revision']}.json"),
                        (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                    )
                )
        else:
            if record["schema"] == 1 and re.fullmatch(r"confirm-A[0-9]*", record["id"]) and not markdown_path.exists():
                raise QuestionRecordError("새 확인 A는 schema 2 정형 초안으로 작성하세요")
            if record["schema"] == 2 and (
                record["answers"] or record["approval"]["status"] == "complete"
                or any(item["status"] != "pending" or item["answer"] for item in record["items"])
            ):
                raise QuestionRecordError("새 schema 2 문서는 미답 상태로 발행하고 답·승인은 answer로 기록하세요")
            if editorial:
                raise QuestionRecordError("editorial 발행에는 기존 질문이 필요함")
            if expected_revision != "":
                raise QuestionRecordError("새 질문에는 expected_revision을 지정할 수 없음")
            if any("selected" in item for item in record["items"]):
                raise QuestionRecordError("새 질문의 selected는 발행 후 answer로 기록해야 함")
            if markdown_path.exists():
                try:
                    legacy_markdown = markdown_path.read_bytes().decode("utf-8")
                except UnicodeError as error:
                    raise QuestionRecordError(f"기존 Markdown UTF-8 형식 오류: {name}") from error
                if record.get("legacy_markdown") != legacy_markdown:
                    raise QuestionRecordError(
                        f"기존 Markdown 보호: legacy_markdown이 없거나 원문이 바뀜: {name}"
                    )
        plan = prepare_outputs(folder, record, current, snapshots)
    except OSError as error:
        raise QuestionRecordError(f"질문 JSON 저장 실패: {path}: {error}") from error
    return record, plan


def preflight_document(folder, record, expected_revision="", editorial=False):
    record, _ = publication_plan(folder, record, expected_revision, editorial)
    return record


def publish_document(folder, record, expected_revision="", editorial=False):
    record, plan = publication_plan(folder, record, expected_revision, editorial)
    commit_outputs(folder, plan)
    return record


def answer_document(folder, name, payload):
    payload = copy.deepcopy(validate_answer(payload))
    record = read_document(folder, name)
    current = copy.deepcopy(record)
    for previous in record["answers"]:
        if previous["id"] == payload["id"]:
            if previous != payload:
                raise QuestionRecordError(f"같은 답 기록 id의 내용이 다름: {payload['id']}")
            return render_saved_document(folder, name)
    if payload["revision"] != record["revision"]:
        raise QuestionRecordError("답 기록 revision이 현재 질문 revision과 다름")
    items = {item["id"]: item for item in record["items"]}
    for update in payload["updates"]:
        if update["id"] not in items:
            raise QuestionRecordError(f"답 대상 문항이 없음: {update['id']}")
        items[update["id"]].update(status=update["status"], answer=update["answer"])
        items[update["id"]].pop("selected", None)
        if "selected" in update:
            items[update["id"]]["selected"] = update["selected"]
    if "approval" in payload:
        record["approval"] = copy.deepcopy(payload["approval"])
    record["answers"].append(payload)
    record = validate_document(record)
    commit_outputs(folder, prepare_outputs(folder, record, current))
    return record


def patch_document(record, changes, editorial=False):
    """Patch a validated deep copy; input objects and publication stay untouched."""
    if type(editorial) is not bool:
        raise QuestionRecordError("editorial은 bool이어야 함")
    record = validate_document(record)
    current = copy.deepcopy(record)
    validate_fields(
        changes,
        (),
        (
            "set",
            "upsert_items",
            "remove_items",
            "reopen_items",
            "upsert_blocks",
            "remove_blocks",
            "upsert_detail_blocks",
            "remove_detail_blocks",
            "upsert_entries",
            "remove_entries",
            "request_approval",
        ),
        "patch",
    )
    if "set" in changes:
        validate_fields(changes["set"], (), ("schema", "revision", "title", "intro", "body"), "patch set")
        if record["schema"] == 2 and changes["set"].get("schema", 2) != 2:
            raise QuestionRecordError("schema 2 문서는 schema 1로 되돌릴 수 없음")
        record.update(copy.deepcopy(changes["set"]))
    reopened = changes.get("reopen_items", [])
    if not isinstance(reopened, list):
        raise QuestionRecordError("reopen_items must be an array")
    for key, target in (("items", "items"), ("blocks", "blocks"), ("detail_blocks", "detail_blocks")):
        upserts = changes.get(f"upsert_{key}", [])
        removals = changes.get(f"remove_{key}", [])
        if not isinstance(upserts, list) or not isinstance(removals, list):
            raise QuestionRecordError(f"patch {key} operations must be arrays")
        seen = set()
        for value in upserts:
            if key == "items":
                validate_fields(
                    value, ("id", "title", "body"), ("heading", "options"), "patch item definition"
                )
            elif not isinstance(value, dict):
                raise QuestionRecordError("patch block must be an object")
            identifier = validate_identifier(value.get("id"), "patch id")
            if identifier in seen:
                raise QuestionRecordError(f"duplicate patch upsert id: {identifier}")
            seen.add(identifier)
        removed = set()
        for identifier in removals:
            validate_identifier(identifier, "patch remove id")
            if identifier in removed or identifier in seen:
                raise QuestionRecordError(f"conflicting patch id: {identifier}")
            removed.add(identifier)
        if not upserts and not removals:
            continue
        values = record.setdefault(target, [])
        indexed = {value["id"]: number for number, value in enumerate(values)}
        if not removed.issubset(indexed):
            raise QuestionRecordError(f"unknown patch remove id: {', '.join(sorted(removed - set(indexed)))}")
        for value in upserts:
            item = copy.deepcopy(value)
            if key == "items":
                item.update(status="pending", answer="")
                if item["id"] in indexed:
                    previous = values[indexed[item["id"]]]
                    definition_changed = any(
                        item.get(field, "") != previous.get(field, "")
                        for field in ("title", "body", "heading", "options")
                    )
                    if definition_changed and item["id"] not in reopened and not editorial:
                        raise QuestionRecordError(
                            f"changed item definition requires explicit reopen_items: {item['id']}"
                        )
                    item.update(status=previous["status"], answer=previous["answer"])
                    if "selected" in previous:
                        item["selected"] = previous["selected"]
            if item["id"] in indexed:
                values[indexed[item["id"]]] = item
            else:
                values.append(item)
        record[target] = [value for value in values if value["id"] not in removed]
        if target == "blocks" and not record[target]:
            del record[target]
    entry_operations = set()
    for operation in ("upsert_entries", "remove_entries"):
        operations = changes.get(operation, [])
        if not isinstance(operations, list):
            raise QuestionRecordError(f"{operation} must be an array")
        for change in operations:
            field = "entry" if operation == "upsert_entries" else "id"
            validate_fields(change, ("block", field), label=operation)
            block = next((value for value in record.get("blocks", []) if value["id"] == change["block"]), None)
            if record["schema"] != 2 or block is None or block["type"] not in ("list", "rules", "steps"):
                raise QuestionRecordError("항목 수정에는 schema 2 본문 list/rules/steps 블록이 필요함")
            identifier = change[field].get("id") if field == "entry" and isinstance(change[field], dict) else change[field]
            validate_identifier(identifier, "entry id")
            key = (change["block"], identifier)
            if key in entry_operations:
                raise QuestionRecordError(f"중복 항목 수정: {key}")
            entry_operations.add(key)
            indexed = {entry["id"]: i for i, entry in enumerate(block["items"])}
            if operation == "remove_entries":
                if identifier not in indexed:
                    raise QuestionRecordError(f"제거할 항목 없음: {key}")
                block["items"].pop(indexed[identifier])
            elif identifier in indexed:
                block["items"][indexed[identifier]] = copy.deepcopy(change[field])
            else:
                block["items"].append(copy.deepcopy(change[field]))
    identifiers = set()
    items = {item["id"]: item for item in record["items"]}
    for identifier in reopened:
        validate_identifier(identifier, "reopen item id")
        if identifier not in items or identifier in identifiers:
            raise QuestionRecordError(f"unknown or duplicate reopen item id: {identifier}")
        identifiers.add(identifier)
        items[identifier].update(status="pending", answer="")
        items[identifier].pop("selected", None)
    if "request_approval" in changes:
        validate_fields(changes["request_approval"], ("reason",), label="request_approval")
        reason = required_text(changes["request_approval"]["reason"], "request_approval.reason", True)
        record["approval"] = {"status": "pending", "reason": reason}
    record = validate_document(record)
    if editorial:
        validate_publication(current, record, True)
    return record
