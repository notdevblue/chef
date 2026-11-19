"""infrastructure / paths."""

import contextlib
import os
import stat
import threading


def normalize(path):
    return os.path.normpath(str(path))


def same_path(first, second):
    if not first or not second:
        return False
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return canonical_path(first) == canonical_path(second)


def canonical_path(path):
    """Resolve aliases in existing parents, including for a not-yet-created path."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def inside_root(root, path):
    """Lexical containment; callers may deliberately distinguish link spellings."""
    root_key = os.path.normcase(os.path.abspath(root))
    path_key = os.path.normcase(os.path.abspath(path))
    try:
        return path_key != root_key and os.path.commonpath([root_key, path_key]) == root_key
    except ValueError:
        return False


def inside_real_root(root, path):
    """Containment after resolving existing aliases, including parent aliases."""
    root_key, path_key = canonical_path(root), canonical_path(path)
    if same_path(root_key, path_key):
        return False
    if inside_root(root_key, path_key):
        return True
    # Case behavior belongs to the filesystem (including case-insensitive Mac
    # volumes), not the OS name. Compare existing parents by their identity.
    parent = os.path.dirname(path_key)
    while True:
        if same_path(root_key, parent):
            return True
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            return False
        parent = next_parent


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
    # This function also normalizes registry glob patterns: do not resolve them.
    # POSIX paths may name different files solely through letter case.
    value = os.path.normcase(os.path.normpath(path))
    return value.replace(os.sep, "/").rstrip("/") or "/"
