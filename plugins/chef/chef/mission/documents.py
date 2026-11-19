"""mission / documents."""

import hashlib
import os
import re

from chef.guards import mission as mission_guard
from chef.infrastructure import paths as infrastructure_paths
from chef.mission.contracts import (
    ANSWER_DATE,
    APPROVED,
    ASK_FILE,
    CONFIRM_FILE,
    QUESTION_FILE,
    SHARED_DOCUMENTS_FILE,
    SHARED_DOCUMENTS_HEADER,
    SKIPPED,
    SYSTEM_DOCUMENTS,
    StateError,
)
from chef.mission.storage import read_table, read_text, write_table
from chef.questions import records as question_records
from chef.questions.summary import (
    QuestionSummary,
    is_answer_section,
    markdown_lines,
    plain_text,
    summarize_question,
)


def canonical_document_path(folder, name):
    raw = str(name).replace("\\", "/")
    parts = raw.split("/")
    if not raw or raw.startswith("/") or os.path.isabs(str(name)) or re.match(r"^[A-Za-z]:", raw):
        raise StateError(f"문서 경로는 미션 폴더 안의 상대 경로여야 함: {name}")
    if any(part in ("", ".", "..") or ":" in part or any(ord(char) < 32 for char in part) for part in parts):
        raise StateError(f"문서 경로 형식 오류: {name}")
    if any(part.startswith(".") for part in parts):
        raise StateError(f"숨김 경로는 공유할 수 없음: {name}")
    canonical = "/".join(parts)
    if canonical in SYSTEM_DOCUMENTS:
        raise StateError(f"시스템 파일은 공유할 수 없음: {name}")
    root = infrastructure_paths.request_realpath(folder)
    target = infrastructure_paths.request_realpath(os.path.join(folder, *parts))
    if not infrastructure_paths.inside_root(root, target):
        raise StateError(f"문서 경로가 미션 폴더 밖이거나 폴더 자체임: {name}")
    resolved = os.path.relpath(target, root).replace(os.sep, "/")
    resolved_parts = resolved.split("/")
    if any(part.startswith(".") for part in resolved_parts):
        raise StateError(f"숨김 경로는 공유할 수 없음: {name}")
    if resolved in SYSTEM_DOCUMENTS:
        raise StateError(f"시스템 파일은 공유할 수 없음: {name}")
    return canonical, target


def shared_document_rows(folder):
    path = os.path.join(folder, SHARED_DOCUMENTS_FILE)
    if not os.path.lexists(path):
        return []
    if not infrastructure_paths.inside_root(
        infrastructure_paths.request_realpath(folder), infrastructure_paths.request_realpath(path)
    ) or not os.path.isfile(path):
        raise StateError(f"{SHARED_DOCUMENTS_FILE} 경로가 잘못됨: {path}")
    try:
        text = read_text(path)
    except UnicodeError as error:
        raise StateError(f"{SHARED_DOCUMENTS_FILE} 인코딩 오류: {error}")
    if not text.splitlines():
        raise StateError(f"{SHARED_DOCUMENTS_FILE} 비어 있음: {path}")
    try:
        rows = read_table(path, SHARED_DOCUMENTS_HEADER)
    except UnicodeError as error:
        raise StateError(f"{SHARED_DOCUMENTS_FILE} 인코딩 오류: {error}")
    seen = set()
    for row in rows:
        canonical, _ = canonical_document_path(folder, row["path"])
        if (
            canonical != row["path"]
            or canonical in seen
            or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
            or not row["at"].strip()
        ):
            raise StateError(f"{SHARED_DOCUMENTS_FILE} 유효하지 않은 행: {row['path']}")
        seen.add(canonical)
    return rows


def shared_documents(folder):
    rows = shared_document_rows(folder)
    shared = set()
    for row in rows:
        _, target = canonical_document_path(folder, row["path"])
        try:
            with open(target, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            continue
        if digest == row["sha256"]:
            shared.add(row["path"])
    return shared


def auto_question_document(folder, name):
    try:
        canonical, target = canonical_document_path(folder, name)
        root = infrastructure_paths.request_realpath(folder)
        resolved = os.path.relpath(target, root).replace(os.sep, "/")
        if os.path.normcase(canonical) != os.path.normcase(resolved) or not os.path.isfile(target):
            return False
        parts = canonical.split("/")
        if len(parts) == 1 and QUESTION_FILE.fullmatch(canonical):
            return True
        if len(parts) == 2 and parts[0] == "details" and QUESTION_FILE.fullmatch(parts[1]):
            return auto_question_document(folder, parts[1])
        if len(parts) == 3 and parts[:2] == ["details", "history"] and QUESTION_FILE.fullmatch(parts[2]):
            return auto_question_document(folder, parts[2])
        return False
    except (OSError, ValueError, StateError):
        return False


def auto_question_documents(folder):
    names = set()
    for name in os.listdir(folder):
        if QUESTION_FILE.fullmatch(name) and auto_question_document(folder, name):
            names.add(name)
            detail = f"details/{name}"
            if auto_question_document(folder, detail):
                names.add(detail)
            history = f"details/history/{name}"
            if auto_question_document(folder, history):
                names.add(history)
    return names


def share_documents(folder, names, expected_hashes=()):
    require_question_integrity(folder)
    requested = list(dict.fromkeys(canonical_document_path(folder, name)[0] for name in names))
    contents = {}
    for name in requested:
        _, target = canonical_document_path(folder, name)
        try:
            with open(target, "rb") as stream:
                content = stream.read()
        except OSError as error:
            raise StateError(f"공유할 파일을 읽을 수 없음: {name} ({error})")
        if not os.path.isfile(target):
            raise StateError(f"공유할 파일이 아님: {name}")
        contents[name] = hashlib.sha256(content).hexdigest()
        if QUESTION_FILE.fullmatch(name):
            try:
                text = content.decode("utf-8-sig")
            except UnicodeError as error:
                raise StateError(f"질문 문서 인코딩 오류: {name} ({error})") from error
            _, issue = question_records.inspect_document(folder, name, text)
            if issue:
                raise StateError(f"{name} 기록 확인 필요: {issue}")
    try:
        expected = dict(expected_hashes)
    except (TypeError, ValueError) as error:
        raise StateError(f"기대 해시 형식 오류: {error}")
    for name, digest in expected.items():
        if name not in contents or digest != contents[name]:
            raise StateError(f"문서가 바뀌었습니다: {name}")
    path = os.path.join(folder, SHARED_DOCUMENTS_FILE)
    rows = shared_document_rows(folder)
    by_name = {row["path"]: row for row in rows}
    for name in requested:
        by_name[name] = {"path": name, "sha256": contents[name], "at": mission_guard.now_iso()}
    ordered = [by_name[row["path"]] for row in rows]
    ordered.extend(by_name[name] for name in requested if name not in {row["path"] for row in rows})
    write_table(path, SHARED_DOCUMENTS_HEADER, ordered)
    return requested


def hide_documents(folder, names):
    requested = list(dict.fromkeys(canonical_document_path(folder, name)[0] for name in names))
    path = os.path.join(folder, SHARED_DOCUMENTS_FILE)
    rows = shared_document_rows(folder)
    hidden = {row["path"] for row in rows} & set(requested)
    if hidden:
        write_table(path, SHARED_DOCUMENTS_HEADER, [row for row in rows if row["path"] not in hidden])
    return [name for name in requested if name in hidden]


def confirm_approved(fields, letter, number):
    whole = fields.get(f"확인 {letter}", "")
    if whole.startswith((APPROVED, SKIPPED)):
        return True
    key = f"{letter}{number}"
    return bool(number) and (
        fields.get(f"확인 {key}", "").startswith(APPROVED)
        or re.search(rf"\b{key}\s*{APPROVED}", whole) is not None
    )


def summarize_question_document(name, text, fields) -> QuestionSummary:
    pending = True
    confirm = CONFIRM_FILE.match(name)
    if confirm:
        pending = not confirm_approved(fields, confirm.group(1).upper(), confirm.group(2))
    elif ASK_FILE.match(name):
        body = markdown_lines(text)
        heading = -1
        for index, line in enumerate(body):
            match = re.match(r"^#{2,3}[ \t]+(.+?)[ \t]*$", line)
            if match and is_answer_section(plain_text(re.sub(r"[ \t]+#+$", "", match.group(1)))):
                heading = index
                break
        below = []
        dated = False
        if heading >= 0:
            for line in body[heading + 1 :]:
                if line.startswith("## "):
                    break
                below.append(line)
            dated = any(ANSWER_DATE.search(line) for line in [body[heading], *below])
        pending = not dated or not any(line.strip() for line in below)
    return summarize_question(name, text, pending)


def question_inspections(folder):
    inspected = []
    try:
        names = set(question_records.document_names(folder))
    except question_records.QuestionRecordError as error:
        names = set()
        inspected.append(("questions", QuestionSummary("questions", "", [], False, False, False), str(error)))
    names.update(name for name in auto_question_documents(folder) if QUESTION_FILE.fullmatch(name))
    for name in sorted(names):
        try:
            _, target = canonical_document_path(folder, name)
            with open(target, "rb") as stream:
                text = stream.read().decode("utf-8-sig")
        except FileNotFoundError:
            text = ""
        except (OSError, UnicodeError, StateError) as error:
            inspected.append(
                (
                    name,
                    QuestionSummary(name, "", [], False, False, False),
                    f"{name} 읽기 실패: {error}; question render {name} 으로 복구할 것",
                )
            )
            continue
        summary, issue = question_records.inspect_document(folder, name, text)
        inspected.append((name, summary, issue))
    return inspected


def unanswered_questions(folder, fields):
    return [name for name, summary, issue in question_inspections(folder) if not issue and summary.pending]


def require_question_integrity(folder):
    issues = [f"{name}: {issue}" for name, _, issue in question_inspections(folder) if issue]
    if issues:
        raise StateError(
            "질문 기록 확인 필요: "
            + "; ".join(issues)
            + "; question check 후 answer/restore/render/publish 로 복구할 것"
        )
