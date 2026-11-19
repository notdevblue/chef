"""guards / logging."""

import json
import os
from datetime import datetime, timezone

from chef.guards import questions as question_write_guard
from chef.guards.contracts import LOG_COMMAND_LENGTH, LOG_ENV, LOG_FIELDS


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_payload(payload):
    command = question_write_guard.input_mapping(payload).get("command")
    entry = {key: payload.get(key) for key in LOG_FIELDS}
    entry["command"] = command[:LOG_COMMAND_LENGTH] if isinstance(command, str) else None
    append_log(entry)


def append_log(entry):
    path = os.environ.get(LOG_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
