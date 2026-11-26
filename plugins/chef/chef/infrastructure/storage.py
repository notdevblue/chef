"""Atomic file replacement with explicit UTF-8 and optional durability."""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path


def replace_file(source, destination):
    """Publish a complete file, allowing a short Windows reader to close.

    Keep the same temporary file and any caller-owned gate across retries.
    Persistent access denial still fails without deleting the old destination.
    """
    deadline = time.monotonic() + 0.5
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            delay = min(0.01, deadline - time.monotonic())
            if delay <= 0:
                raise
            time.sleep(delay)


def atomic_write_bytes(path, value, *, durable=False, create_parents=False, creation_mode=None):
    path = Path(path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    if creation_mode is None:
        descriptor, filename = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(filename)
    else:
        # os.open applies the caller's umask, as the former text writers did.
        # chmod on a mkstemp file would bypass that policy.
        while True:
            temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                descriptor = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                    creation_mode,
                )
                break
            except FileExistsError:
                continue
    try:
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write(value)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        replace_file(temporary, path)
        if durable and os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path, value, *, newline="\n", **options):
    if newline is None:
        value = value.replace("\n", os.linesep)
    elif newline != "\n":
        raise ValueError("newline must be LF or the native text mode (None)")
    atomic_write_bytes(path, value.encode("utf-8"), **options)


def atomic_write_json(path, value, **options):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", **options)
