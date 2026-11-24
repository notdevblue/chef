import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from chef.infrastructure.locking import FileLock
from chef.infrastructure.paths import normalize_path


class ReferenceError(ValueError):
    pass


def text_value(value, label):
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
    ):
        raise ReferenceError(f"{label}: 비어 있지 않은 한 줄 문자열이 필요함")
    return value


def resolve_path(project, value, external=False):
    text_value(value, "path")
    raw = value.replace("\\", "/") if os.name == "nt" else value
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


def snapshot(project, sources, output, revision="", section="", expected_sha256="", freeze=False):
    destination = resolve_path(project, output)
    # Keep CAS serialized across CLI/API callers without adding files to source trees.
    key = hashlib.sha256(normalize_path(destination).encode("utf-8")).hexdigest()
    lock = Path(tempfile.gettempdir()) / "chef-reference-locks" / f"{key}.lock"
    with FileLock(lock, "reference snapshot", timeout=2):
        return _snapshot(project, sources, output, revision, section, expected_sha256, freeze)


def frozen_copy(project, destination, pointer, source):
    relative = destination.parent.relative_to(Path(project).resolve()) / ".reference-objects" / "sha256" / pointer["sha256"]
    target = resolve_path(project, relative.as_posix())
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as stream:
            temporary = stream.name
            copied_hash = hashlib.sha256()
            with source.open("rb") as original:
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    stream.write(chunk)
                    copied_hash.update(chunk)
        if copied_hash.hexdigest() != pointer["sha256"]:
            raise ReferenceError(f"source 가 snapshot 중 바뀜: {pointer['path']}")
        try:
            # Publish only absent objects. Existing content-addressed objects are never replaced.
            os.link(temporary, target)
        except FileExistsError:
            pass
        if file_pointer(project, relative.as_posix())["sha256"] != pointer["sha256"]:
            raise ReferenceError(f"frozen object hash 불일치: {relative.as_posix()}")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": relative.as_posix(), "sha256": pointer["sha256"]}


def _snapshot(project, sources, output, revision="", section="", expected_sha256="", freeze=False):
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
        record = {**pointer, **metadata}
        if freeze:
            record["frozen"] = frozen_copy(project, destination, pointer, resolved)
        pointers.append(record)
    bundle = {"schema": 1, "sources": pointers}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=destination.parent, delete=False
        ) as stream:
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
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"schema", "sources"}
        or type(bundle["schema"]) is not int
        or bundle["schema"] != 1
    ):
        raise ReferenceError("reference schema 1의 schema/sources 필드만 허용됨")
    sources = bundle["sources"]
    if not isinstance(sources, list) or not sources:
        raise ReferenceError("reference sources 는 비어 있지 않은 배열이어야 함")
    seen = set()
    for source in sources:
        if (
            not isinstance(source, dict)
            or not {"path", "sha256"}.issubset(source)
            or set(source) - {"path", "sha256", "revision", "section", "frozen"}
        ):
            raise ReferenceError("reference source 는 path/sha256 및 선택적 revision/section/frozen만 허용됨")
        resolved = resolve_path(project, source["path"], external=True)
        if resolved in seen:
            raise ReferenceError(f"중복 reference source: {source['path']}")
        seen.add(resolved)
        if not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ReferenceError("reference sha256 형식 오류")
        if "frozen" in source:
            frozen = source["frozen"]
            if (not isinstance(frozen, dict) or set(frozen) != {"path", "sha256"}
                    or frozen.get("sha256") != source["sha256"]):
                raise ReferenceError("frozen pointer 형식 오류")
            copied = resolve_path(project, frozen["path"])
            if tuple(copied.parts[-3:]) != (".reference-objects", "sha256", source["sha256"]):
                raise ReferenceError("frozen pointer 는 content-addressed object여야 함")
        for name in ("revision", "section"):
            if name in source:
                text_value(source[name], name)
    return bundle


def check(project, value):
    bundle = load(project, value)
    missing, changed = [], []
    live_missing, live_changed = [], []
    frozen_count = 0
    for source in bundle["sources"]:
        pointer = source.get("frozen", source)
        external = "frozen" not in source
        path = resolve_path(project, pointer["path"], external=external)
        if not path.is_file():
            missing.append(pointer["path"])
        elif file_pointer(project, pointer["path"], external=external)["sha256"] != pointer["sha256"]:
            changed.append(pointer["path"])
        if "frozen" in source:
            frozen_count += 1
            original = resolve_path(project, source["path"], external=True)
            if not original.is_file():
                live_missing.append(source["path"])
            elif file_pointer(project, source["path"], external=True)["sha256"] != source["sha256"]:
                live_changed.append(source["path"])
    status = "pass"
    if missing or changed:
        status = "fail"
    result = {
        "schema": 1,
        "status": status,
        "sources": bundle["sources"],
        "missing": missing,
        "changed": changed,
    }
    if frozen_count:
        result["liveSources"] = {"missing": live_missing, "changed": live_changed}
    return result


def require_current(project, value):
    result = check(project, value)
    if result["status"] == "fail":
        raise ReferenceError(
            f"reference 변경/누락: changed={result['changed']} missing={result['missing']}; 원본을 확인하고 snapshot을 갱신할 것"
        )
    return result


def read_sources(bundle):
    """Explicit effective read targets, separate from original provenance."""
    return [{**source.get("frozen", {key: source[key] for key in ("path", "sha256")}),
             "mode": "frozen" if "frozen" in source else "live",
             "provenance": {key: value for key, value in source.items() if key != "frozen"}}
            for source in bundle["sources"]]
