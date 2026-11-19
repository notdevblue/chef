"""infrastructure / paths."""

import contextlib
import os
import stat
import threading


def normalize(path):
    return os.path.normpath(str(path))


def same_path(first, second):
    return normalize_path(first) == normalize_path(second)


def inside_root(root, path):
    root_key = os.path.normcase(os.path.abspath(root))
    path_key = os.path.normcase(os.path.abspath(path))
    try:
        return path_key != root_key and os.path.commonpath([root_key, path_key]) == root_key
    except ValueError:
        return False


REQUEST_PATHS = threading.local()


@contextlib.contextmanager
def request_realpaths():
    nested = hasattr(REQUEST_PATHS, "memo")
    if not nested:
        REQUEST_PATHS.memo = {}
    try:
        yield
    finally:
        if not nested:
            del REQUEST_PATHS.memo


def request_realpath(path):
    memo = getattr(REQUEST_PATHS, "memo", {})
    key = os.fspath(path)
    if key not in memo:
        memo[key] = os.path.realpath(key)
    return memo[key]


def is_link(path):
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        return isjunction(path)
    if os.name == "nt":
        # Python 3.11 has no isjunction(), and islink() excludes junctions.
        # Inspect the node itself: following it could authorize removing the
        # real workspace behind a junction. Refuse any Windows reparse node.
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except (OSError, ValueError, TypeError):
            return False
        return (
            type(attributes) is int
            and attributes >= 0
            and bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        )
    return False


def normalize_path(path):
    return os.path.normpath(path).replace("\\", "/").rstrip("/").lower()
