"""Carry the running Python into child CLI hooks without a system-wide alias."""

import hashlib
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path


def python_environment(environment=None, cache_root=None):
    """Return a child-only PATH; never change the parent environment or Python.

    POSIX distributions can provide either Python command. Put both names for
    this interpreter in front of PATH so child hooks use the same environment.
    The launchers also survive a terminal launcher exiting.
    Existing files are checked, never replaced.
    """
    if sys.version_info < (3, 11):
        raise ValueError("Chef requires Python 3.11 or newer")
    env = dict(os.environ if environment is None else environment)
    executable = Path(sys.executable).absolute()
    if not executable.is_file():
        raise ValueError("the running Python executable is unavailable")
    if os.name == "nt":
        if executable.name.lower() != "python.exe":
            raise ValueError("use a Windows Python installation providing python.exe")
        directory = executable.parent
    else:
        if cache_root is None:
            cache_root = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "chef/interpreters"
        key = hashlib.sha256(("launcher-v1\0" + str(executable) + "\0" + sys.version).encode()).hexdigest()[:24]
        directory = Path(cache_root) / key
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for folder in (Path(cache_root).resolve(), directory):
            metadata = folder.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise ValueError(f"Chef Python cache must be an owned directory without group/other write access: {folder}")
        for name in ("python", "python3"):
            _install_alias(directory / name, executable)
    env["PATH"] = str(directory) + os.pathsep + env.get("PATH", os.defpath)
    return env


def _install_alias(alias, executable):
    directory = alias.parent
    # A symlink through another bin directory can lose pyvenv.cfg discovery.
    # exec the original interpreter path so virtualenv packages stay visible.
    content = "#!/bin/sh\nexec " + shlex.quote(str(executable)) + ' "$@"\n'
    descriptor, temporary = tempfile.mkstemp(prefix=f".{alias.name}-", dir=directory)
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, alias)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    metadata = alias.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError(f"Chef Python alias already exists with another target: {alias}")
    expected = content.encode("utf-8")
    with alias.open("rb") as stream:
        actual = stream.read(len(expected) + 1)
    if actual != expected or not os.access(alias, os.X_OK):
        raise ValueError(f"Chef Python alias already exists with another target: {alias}")
