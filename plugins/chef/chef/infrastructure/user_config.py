"""Optional user-wide Chef preferences, shared by runtime installations."""

import json
from pathlib import Path

MAX_BYTES = 1024 * 1024


def path():
    return Path.home() / ".chef" / "config.json"


def read():
    source = path()
    try:
        with source.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(payload) > MAX_BYTES:
        raise ValueError(f"Chef global configuration exceeds 1 MiB: {source}")
    try:
        value = json.loads(payload)
    except ValueError as error:
        raise ValueError(f"Invalid Chef global configuration: {source}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Chef global configuration must be an object: {source}")
    return value
