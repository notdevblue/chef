"""Hermes profile paths for native calls and standalone Chef processes."""

import os
from pathlib import Path


def process_home():
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def active_home():
    try:
        from hermes_constants import get_hermes_home
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_constants":
            raise
        return process_home()
    return Path(get_hermes_home())
