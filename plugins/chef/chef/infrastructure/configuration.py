"""Resolve validated project configuration for commands and services."""

import os

from chef.guards import adapters as mission_guard
from chef.infrastructure.errors import UsageError


def read_adapter(adapter_path, runtime="claude"):
    problems = []
    try:
        adapter = mission_guard.load_adapter(adapter_path, problems, runtime=runtime)
    except mission_guard.GuardOff as e:
        raise UsageError(str(e))
    if problems:
        raise UsageError("; ".join(problems))
    return adapter


def locate(project_arg, runtime="claude"):
    saved = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_arg:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    try:
        located = mission_guard.locate_adapter(
            {"cwd": os.path.abspath(project_arg or os.getcwd()), "han_runtime": runtime}
        )
    except mission_guard.GuardOff as e:
        raise UsageError(str(e))
    finally:
        if saved is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = saved
    if not located:
        raise UsageError("어댑터를 찾지 못함 (--project <경로>, HAN_ADAPTERS_DIR, 레지스트리 roots 확인)")
    return located
