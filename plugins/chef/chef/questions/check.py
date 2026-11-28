"""Check current document contracts and evidence; semantic review remains required."""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from chef.locations import PLUGIN_ROOT
from chef.questions import records as question_records
from chef.questions import readability
from chef.questions.summary import (
    answer_recorded,
    is_answer_section,
    markdown_lines,
    plain_text,
)


def without_fences(text):
    lines = []
    fence = ""
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)[0]
            if not fence:
                fence = marker
            elif marker == fence:
                fence = ""
            continue
        if not fence:
            lines.append(line)
    return "\n".join(lines)


def answer(text):
    match = re.search(r"^## (?:답|답변)\s*\n", text, re.M)
    if match:
        return text[match.start() :].strip()
    return ""


def answer_bodies(text):
    headings = []
    for match in re.finditer(r"(?m)^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*\r?$", text):
        visible = markdown_lines(text[: match.end()])
        if not visible or visible[-1] != match.group(0).rstrip("\r"):
            continue
        value = plain_text(re.sub(r"[ \t]+#+$", "", match.group(2)))
        headings.append((match.start(), match.end(), len(match.group(1)), value))
    bodies = []
    for index, (_, start, level, title) in enumerate(headings):
        if level not in (2, 3) or not is_answer_section(title):
            continue
        end = len(text)
        for next_start, _, next_level, _ in headings[index + 1 :]:
            if next_level <= level:
                end = next_start
                break
        body = question_records.normalize_markdown(text[start:end]).strip("\n")
        visible = [plain_text(line).replace("답변대기", "답변 대기") for line in markdown_lines(body)]
        if answer_recorded(["## 답", *visible]):
            dates = re.findall(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", title)
            bodies.append((body, dates))
    return bodies


def history_date_preserved(value, answers):
    year, month, day = (int(part) for part in re.split(r"[-./]", value))
    normalized = f"{year:04}-{month:02}-{day:02}"
    for item in answers:
        if (
            value in item["text"]
            or datetime.fromisoformat(item["at"].replace("Z", "+00:00")).date().isoformat() == normalized
        ):
            return True
    return False


def read_document(path):
    if path.suffix.lower() == ".json":
        record = question_records.validate_document(question_records.read_json(path))
        return question_records.render_document(record), record
    return path.read_text(encoding="utf-8-sig"), {}


def check_record_history(record, source_record, issues):
    if record["id"] != source_record["id"]:
        issues.append("원문과 질문 문서 id가 다릅니다")
    source_answers = source_record["answers"]
    if record["answers"][: len(source_answers)] != source_answers:
        issues.append("기존 답 원문 이력이 삭제되거나 달라졌습니다")
    if "legacy_markdown" in source_record and (
        "legacy_markdown" not in record or record["legacy_markdown"] != source_record["legacy_markdown"]
    ):
        issues.append("기존 legacy_markdown이 삭제되거나 달라졌습니다")


def check_migration(record, text, source_path, issues):
    original = source_path.read_bytes().decode("utf-8")
    if "legacy_markdown" not in record or record["legacy_markdown"] != original:
        issues.append("legacy_markdown이 원문 Markdown과 다릅니다: BOM과 줄바꿈까지 그대로 보존하세요")
    rendered = question_records.normalize_markdown(question_records.render_record_history(record))
    for body, dates in answer_bodies(original):
        matching_answers = [
            item for item in record["answers"] if body in question_records.normalize_markdown(item["text"])
        ]
        if not matching_answers or body not in rendered:
            issues.append(
                "기존 답 절 본문이 답 원문 이력에서 빠지거나 달라졌습니다: 원문의 답과 날짜를 그대로 보존하세요"
            )
        if any(not history_date_preserved(value, matching_answers) for value in dates):
            issues.append("기존 답 절 제목의 날짜가 답 원문 이력의 날짜나 원문에서 빠지거나 달라졌습니다")


def schema_tables(text):
    tables = []
    group = []
    for line in [*without_fences(text).splitlines(), ""]:
        if line.lstrip().startswith("|"):
            group.append(line.strip())
            continue
        if len(group) > 1:
            header = group[0]
            if re.search(r"타입|컬럼|요청|응답|필드|Enum|enum|값|자료형", header) and not re.search(
                r"파일|경로|검증 명령", header
            ):
                tables.append("\n".join(group))
        group = []
    return tables


def current_field_blocks(record):
    return [block for block in record.get("blocks", []) if block["type"] in ("fields", "schema-table")]


def protected_hidden_blocks(record, field):
    return [
        {key: value for key, value in block.items() if key != "title"}
        for block in record.get(field, [])
        if block["type"] in ("table", "fields", "schema-table", "literal")
    ]


def check_hidden_contract(record, source_record, issues):
    if record.get("presentation") not in ("decision-pending", "superseded"):
        return
    if record.get("body") != source_record.get("body"):
        issues.append("숨김 표시 문서가 source body 원문을 변경했습니다")
    for field in ("blocks", "detail_blocks"):
        if protected_hidden_blocks(record, field) != protected_hidden_blocks(source_record, field):
            issues.append(f"숨김 표시 문서의 {field} table/fields/literal 정본이 source 원문과 다릅니다")


def legacy_table_preserved_by_fields(table, blocks):
    """Prove known legacy columns from actual definitions, without guessing schema headers."""
    names = {
        "필드": "name",
        "컬럼": "name",
        "이름": "name",
        "타입": "type",
        "자료형": "type",
        "키": "key",
        "참조": "ref",
        "설명": "description",
    }
    lines = table.splitlines()
    if len(lines) < 3 or "\\" in table:
        return False
    rows = [[value.strip().strip("`") for value in line.strip().strip("|").split("|")] for line in lines]
    columns = [names.get(value) for value in rows[0]]
    if None in columns or "name" not in columns or "type" not in columns or len(columns) != len(set(columns)):
        return False
    if any(not re.fullmatch(r":?-+:?", value) for value in rows[1]) or len(rows[1]) != len(columns):
        return False
    if any(len(row) != len(columns) for row in rows[2:]):
        return False
    for block in blocks:
        definitions = {definition["name"]: definition for definition in block["fields"]}
        preserved = True
        for row in rows[2:]:
            expected = dict(zip(columns, row))
            definition = definitions.get(expected["name"])
            if definition is None or any(
                definition.get(name, "") != value for name, value in expected.items()
            ):
                preserved = False
                break
        if preserved:
            return True
    return False


def prose_length(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*\|.*(?:\n|$)", "", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", text)
    return len(text.strip())


def check(path, source_path="", archive_path=""):
    text = ""
    issues = []
    record = {}
    try:
        text, record = read_document(path)
        if source_path:
            source, source_record = read_document(source_path)
            archived = False
            candidate = archive_path
            if (
                not candidate
                and source_record
                and source_path.parent.name == "questions"
                and source_path.name == f"{source_record['id']}.json"
            ):
                inferred = question_records.history_path(
                    source_path.parent.parent, f"{source_record['id']}.md"
                )
                if inferred.exists():
                    candidate = inferred
            if candidate:
                try:
                    if not source_record or (record and source_record["id"] != record["id"]):
                        raise question_records.QuestionRecordError(
                            "전체 이력 보존 검사는 같은 질문 id의 JSON 원문이 필요함"
                        )
                    archived = question_records.validate_history_archive(candidate, source_record)
                except (question_records.QuestionRecordError, OSError, UnicodeError) as error:
                    issues.append(f"전체 이력 보존 검사 실패: {error}")
            if path.suffix.lower() == ".json":
                if source_path.suffix.lower() == ".json":
                    check_record_history(record, source_record, issues)
                    check_hidden_contract(record, source_record, issues)
                else:
                    check_migration(record, text, source_path, issues)
            elif source_path.suffix.lower() == ".json":
                if question_records.matches_current_render(source_record, text):
                    record = source_record
                else:
                    issues.append("생성 Markdown이 원본 질문 JSON의 렌더 결과와 다릅니다")
            elif answer(source) != answer(text):
                issues.append("기존 답 절이 달라졌습니다: 원문의 답과 날짜를 그대로 보존하세요")
            current_tables = schema_tables(text)
            current_fields = current_field_blocks(record)
            current_contract = bool(current_tables or current_fields)
            hidden_contract = bool(
                record and record.get("presentation") in ("decision-pending", "superseded")
            )
            if hidden_contract and not archived:
                issues.append("숨김 표시 문서는 원본 정본과 일치하는 전체 이력이 필요합니다")
            source_tables = source
            if source_record:
                source_tables = question_records.render_record_history({**source_record, "detail_blocks": []})
            for number, table in enumerate(schema_tables(source_tables), 1):
                preserved = table in current_tables or legacy_table_preserved_by_fields(table, current_fields)
                if not preserved and not (hidden_contract and archived) and (not archived or not current_contract):
                    issues.append(f"원문 설계 표 {number}의 컬럼·타입·조건이 빠지거나 달라졌습니다")
            for block in current_field_blocks(source_record):
                preserved = any(
                    candidate["id"] == block["id"] and candidate["fields"] == block["fields"]
                    for candidate in current_fields
                )
                if not preserved and not (hidden_contract and archived) and (not archived or not current_contract):
                    issues.append(f"원문 필드 정의 {block['id']}의 컬럼·타입·조건이 빠지거나 달라졌습니다")
        elif archive_path:
            issues.append("전체 이력 보존 검사에는 --source가 필요합니다")
    except (question_records.QuestionRecordError, OSError, UnicodeError) as error:
        issues.append(f"문서 검사 실패: {error}")
    return {
        "path": str(path),
        "prose_characters": prose_length(text),
        "issues": issues,
        "decision_review": question_records.decision_review_notes(record) if record else [],
        "readability": readability.analyze(record) if record else readability.analyze_markdown(text),
        "semantic_review_required": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="사람용 문서의 설계 표·기존 답을 검사합니다. 문체와 의미 대조는 별도입니다."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--archive", type=Path, help="정본과 대조할 details/history/<id>.md 전체 이력")
    args = parser.parse_args(argv)
    result = check(args.path, args.source, args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result["issues"]))


if __name__ == "__main__":
    raise SystemExit(main())
