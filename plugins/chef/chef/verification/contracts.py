"""verification / contracts."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from chef.infrastructure.storage import atomic_write_json

RUNNER_VERSION = 2


SEVERITIES = ("critical", "high", "medium", "low")


PASSING_REVIEW_STATUSES = ("clean", "closed-low")


COPY_LIMIT = 1024 * 1024


COPY_BUDGET = 64 * 1024 * 1024


WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *[f"com{index}" for index in range(1, 10)],
    *[f"lpt{index}" for index in range(1, 10)],
}


class VerificationError(Exception):
    def __init__(self, message, category="result_invalid"):
        super().__init__(message)
        self.category = category


class ScopeDiffError(VerificationError):
    def __init__(self, message, changed):
        super().__init__(message)
        self.changed = changed


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise VerificationError(f"JSON 읽기 실패: {path}: {error}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"JSON 객체가 필요함: {path}")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


INSPECT_ITEMS = 8


INSPECT_CHARS = 240


def write_json(path, value):
    atomic_write_json(path, value, newline=None, creation_mode=0o666)


def utc_now():
    return datetime.now(timezone.utc).isoformat()
