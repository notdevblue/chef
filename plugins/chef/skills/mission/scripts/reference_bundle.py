import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


class ReferenceError(ValueError):
    pass


def text_value(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise ReferenceError(f"{label}: 비어 있지 않은 한 줄 문자열이 필요함")
    return value


def resolve_path(project, value, external=False):
    text_value(value, "path")
    raw = value.replace("\\", "/")
    if ".." in raw.split("/") or ":" in raw[2:] or (":" in raw and not re.match(r"^[A-Za-z]:/", raw)):
        raise ReferenceError(f"path 형식 오류: {value}")
    root = Path(project).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
    except (OSError, RuntimeError) as error:
        raise ReferenceError(f"path 확인 실패: {value}: {error}") from error
    if not external and (not path.is_relative_to(root) or path == root):
        raise ReferenceError(f"프로젝트 밖 path: {value}")
    return path


def file_pointer(project, value, external=False):
    path = resolve_path(project, value, external)
    if not path.is_file():
        raise ReferenceError(f"파일 없음: {value}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    root = Path(project).resolve()
    label = str(path)
    if path.is_relative_to(root):
        label = path.relative_to(root).as_posix()
    return {"path": label, "sha256": digest}


def validate_destination(project, output, expected_sha256):
    destination = resolve_path(project, output)
    if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ReferenceError("--expect-sha256 형식 오류")
    if destination.exists():
        load(project, output)
        if not expected_sha256:
            raise ReferenceError("기존 reference 갱신에는 --expect-sha256 이 필요함")
        if file_pointer(project, output)["sha256"] != expected_sha256:
            raise ReferenceError("기존 reference hash 가 --expect-sha256 과 다름; 현재 묶음을 다시 확인할 것")
    elif expected_sha256:
        raise ReferenceError("--expect-sha256 대상 reference 파일 없음")


def snapshot(project, sources, output, revision="", section="", expected_sha256=""):
    if not sources:
        raise ReferenceError("--source 파일을 하나 이상 지정할 것")
    destination = resolve_path(project, output)
    if Path(output).is_absolute():
        raise ReferenceError("--output 은 프로젝트 안 상대 파일 경로여야 함")
    validate_destination(project, output, expected_sha256)
    metadata = {}
    for name, value in (("revision", revision), ("section", section)):
        if value:
            metadata[name] = text_value(value, name)
    pointers = []
    seen = set()
    for source in sources:
        resolved = resolve_path(project, source, external=True)
        if resolved == destination:
            raise ReferenceError("snapshot 출력은 source 파일을 덮어쓸 수 없음")
        pointer = file_pointer(project, source, external=True)
        if resolved in seen:
            raise ReferenceError(f"중복 source: {source}")
        seen.add(resolved)
        pointers.append({**pointer, **metadata})
    bundle = {"schema": 1, "sources": pointers}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        validate_destination(project, output, expected_sha256)
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return bundle


def load(project, value):
    path = resolve_path(project, value)
    try:
        bundle = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ReferenceError(f"reference JSON 읽기 실패: {value}: {error}") from error
    if not isinstance(bundle, dict) or set(bundle) != {"schema", "sources"} or type(bundle["schema"]) is not int or bundle["schema"] != 1:
        raise ReferenceError("reference schema 1의 schema/sources 필드만 허용됨")
    sources = bundle["sources"]
    if not isinstance(sources, list) or not sources:
        raise ReferenceError("reference sources 는 비어 있지 않은 배열이어야 함")
    seen = set()
    for source in sources:
        if (not isinstance(source, dict) or not {"path", "sha256"}.issubset(source)
                or set(source) - {"path", "sha256", "revision", "section"}):
            raise ReferenceError("reference source 는 path/sha256 및 선택적 revision/section만 허용됨")
        resolved = resolve_path(project, source["path"], external=True)
        if resolved in seen:
            raise ReferenceError(f"중복 reference source: {source['path']}")
        seen.add(resolved)
        if not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ReferenceError("reference sha256 형식 오류")
        for name in ("revision", "section"):
            if name in source:
                text_value(source[name], name)
    return bundle


def check(project, value):
    bundle = load(project, value)
    missing, changed = [], []
    for source in bundle["sources"]:
        path = resolve_path(project, source["path"], external=True)
        if not path.is_file():
            missing.append(source["path"])
        elif file_pointer(project, source["path"], external=True)["sha256"] != source["sha256"]:
            changed.append(source["path"])
    status = "pass"
    if missing or changed:
        status = "fail"
    return {"schema": 1, "status": status,
            "sources": bundle["sources"], "missing": missing, "changed": changed}


def require_current(project, value):
    result = check(project, value)
    if result["status"] == "fail":
        raise ReferenceError(f"reference 변경/누락: changed={result['changed']} missing={result['missing']}; 원본을 확인하고 snapshot을 갱신할 것")
    return result
