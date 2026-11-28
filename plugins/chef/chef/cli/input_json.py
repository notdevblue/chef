"""Read an explicit, bounded JSON object from a CLI's standard input."""

import json
import sys


def stdin_object(limit=1024 * 1024):
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise ValueError("standard input JSON exceeds its size limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("standard input JSON must be an object")
    return result
