"""Atomic registry publication during temporary Windows read sharing."""

import os
import threading

import pytest

from chef.infrastructure import storage
from chef.verification import execution


@pytest.mark.skipif(os.name != "nt", reason="Windows read sharing")
def test_registry_publishes_after_a_live_reader_releases_its_handle(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"value": "previous"}')
    stream = path.open("rb")
    assert stream.read(1) == b"{"
    release = threading.Timer(0.05, stream.close)
    release.start()
    try:
        storage.atomic_write_json(path, {"value": "current"})
    finally:
        release.join()
        stream.close()
    assert execution.read_record(path) == {"value": "current"}
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing error codes")
@pytest.mark.parametrize("code", [5, 32, 33])
def test_permanent_sharing_denial_is_bounded_and_preserves_previous_bytes(tmp_path, monkeypatch, code):
    path = tmp_path / "state.json"
    path.write_bytes(b"previous")
    clock = [0.0]

    def deny(*args):
        error = PermissionError("persistent sharing denial")
        error.winerror = code
        raise error

    monkeypatch.setattr(storage.os, "replace", deny)
    monkeypatch.setattr(storage.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(storage.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    with pytest.raises(PermissionError, match="persistent sharing denial"):
        storage.atomic_write_json(path, {"value": "current"})
    assert clock[0] == pytest.approx(0.5)
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]
