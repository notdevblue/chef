"""A contender must report contention even when an empty gate is already locked."""
import os

import pytest
from chef.infrastructure.locking import FileLock, LockBusy


def test_empty_gate_contender_does_not_write_into_another_owners_locked_range(tmp_path):
    path = tmp_path / "shared.lock"
    gate = tmp_path / "shared.lock.gate"
    descriptor = os.open(gate, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockBusy):
            with FileLock(path, "fictional contender"):
                pytest.fail("contender entered another owner's critical section")
        assert gate.stat().st_size == 0
        assert not path.exists()
    finally:
        os.close(descriptor)
