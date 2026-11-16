"""Real process coverage for stale-marker/kernel-gate coordination."""
import multiprocessing
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import slots
import verification


def _nothing():
    pass


def _hold(path, entered, release, outcomes, pause_remove=None, allow_remove=None):
    if pause_remove is not None:
        remove = slots.os.remove
        intercepted = False

        def delayed_remove(target):
            nonlocal intercepted
            if str(target) == path and not intercepted:
                intercepted = True
                pause_remove.set()
                if not allow_remove.wait(10):
                    raise RuntimeError("stale reclamation barrier timed out")
            return remove(target)
        slots.os.remove = delayed_remove
    try:
        with slots.FileLock(path, "synthetic process"):
            outcomes.put(("entered", os.getpid()))
            entered.set()
            if not release.wait(10):
                raise RuntimeError("critical section barrier timed out")
    except slots.LockBusy:
        outcomes.put(("busy", os.getpid()))


@pytest.fixture
def processes():
    context = multiprocessing.get_context("spawn")
    children = []
    retained_arguments = []

    def start(target, *args):
        # Spawn rebuilds synchronization objects asynchronously; retain even
        # temporary Event arguments until their children have finished.
        retained_arguments.append(args)
        process = context.Process(target=target, args=args)
        process.start()
        children.append(process)
        return process
    yield context, start
    for process in children:
        if process.is_alive():
            process.terminate()
        process.join(5)
        assert not process.is_alive()


def test_two_stale_reclaimers_cannot_unlink_replacement_live_holder(tmp_path, processes):
    context, start = processes
    dead = start(_nothing)
    dead.join(5)
    assert dead.exitcode == 0 and not slots.process_alive(dead.pid)
    path = str(tmp_path / "shared.lock")
    Path(path).write_text(f"{dead.pid}\tfictional stale marker\n")
    stale_checked, allow_unlink, owner_entered, release = (context.Event() for _ in range(4))
    outcomes = context.Queue()
    owner = start(_hold, path, owner_entered, release, outcomes, stale_checked, allow_unlink)
    try:
        assert stale_checked.wait(5)
        # The old implementation lets this contender reclaim/enter while the
        # first process is paused after its stale check, then loses its marker.
        contender = start(_hold, path, context.Event(), release, outcomes)
        assert outcomes.get(timeout=5) == ("busy", contender.pid)
        contender.join(5)
        allow_unlink.set()
        assert owner_entered.wait(5)
        assert outcomes.get(timeout=5) == ("entered", owner.pid)
        assert Path(path).read_text().startswith(f"{owner.pid}\t")
        second = start(_hold, path, context.Event(), release, outcomes)
        assert outcomes.get(timeout=5) == ("busy", second.pid)
        assert Path(path).read_text().startswith(f"{owner.pid}\t")
    finally:
        allow_unlink.set()
        release.set()
    owner.join(5)
    assert owner.exitcode == 0
    assert not Path(path).exists()
    assert Path(path + ".gate").exists()


def test_killed_owner_releases_kernel_gate_and_dead_pid_is_reclaimed(tmp_path, processes):
    context, start = processes
    path = str(tmp_path / "shared.lock")
    entered, release, outcomes = context.Event(), context.Event(), context.Queue()
    owner = start(_hold, path, entered, release, outcomes)
    assert entered.wait(5) and outcomes.get(timeout=5)[0] == "entered"
    gate = Path(path + ".gate")
    identity = gate.stat().st_ino
    owner.terminate()
    owner.join(5)
    assert not owner.is_alive() and Path(path).exists()
    with slots.FileLock(path, "recovered"):
        assert Path(path).read_text().startswith(f"{os.getpid()}\t")
        assert gate.stat().st_ino == identity
    assert not Path(path).exists() and gate.stat().st_ino == identity


def test_resource_lock_still_refuses_dead_marker_after_kernel_release(tmp_path, processes):
    context, start = processes
    path = str(tmp_path / "resource.lock")
    entered, release, outcomes = context.Event(), context.Event(), context.Queue()
    owner = start(_hold, path, entered, release, outcomes)
    assert entered.wait(5) and outcomes.get(timeout=5)[0] == "entered"
    owner.terminate()
    owner.join(5)
    original = Path(path).read_bytes()
    with pytest.raises(slots.LockBusy):
        with verification.ResourceLock(path, "resource"):
            pytest.fail("ResourceLock reclaimed a dead holder")
    assert Path(path).read_bytes() == original
    # A refusal must close its gate descriptor and release its thread lock.
    with slots.FileLock(path, "explicit ordinary recovery"):
        pass


def test_aliases_share_canonical_thread_and_kernel_gate(tmp_path):
    directory = tmp_path / "actual"
    directory.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(directory, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    path = directory / "same.lock"
    other = alias / "same.lock"
    with slots.FileLock(path, "owner"):
        with pytest.raises(slots.LockBusy):
            with slots.FileLock(other, "alias"):
                pytest.fail("alias entered concurrently")
    assert not path.exists() and Path(str(path) + ".gate").is_file()


@pytest.mark.parametrize("phase", ["gate", "marker", "exit"])
def test_baseexception_closes_descriptors_and_thread_lock(tmp_path, monkeypatch, phase):
    path = str(tmp_path / "interrupt.lock")
    lock = slots.FileLock(path, "interrupted")
    original_gate = lock.acquire_gate
    original_remove = slots.os.remove

    def interrupted_gate():
        original_gate()
        raise KeyboardInterrupt()

    def interrupted_marker():
        raise KeyboardInterrupt()

    def interrupted_remove(target):
        if str(target) == path:
            raise KeyboardInterrupt()
        return original_remove(target)

    with monkeypatch.context() as patch:
        if phase == "gate":
            patch.setattr(lock, "acquire_gate", interrupted_gate)
        elif phase == "marker":
            patch.setattr(lock, "acquire", interrupted_marker)
        else:
            patch.setattr(slots.os, "remove", interrupted_remove)
        with pytest.raises(KeyboardInterrupt):
            with lock:
                pass
    assert lock.gate_fd is None
    with slots.FileLock(path, "after interruption"):
        pass


def test_direct_pid_acquire_without_kernel_gate_is_rejected(tmp_path):
    path = tmp_path / "direct.lock"
    with pytest.raises(RuntimeError, match="context manager"):
        slots.FileLock(path, "direct").acquire()
    assert not path.exists()


def test_live_kernel_gate_contention_respects_timeout(tmp_path, processes):
    context, start = processes
    path = str(tmp_path / "bounded.lock")
    entered, release, outcomes = context.Event(), context.Event(), context.Queue()
    owner = start(_hold, path, entered, release, outcomes)
    assert entered.wait(5) and outcomes.get(timeout=5)[0] == "entered"
    started = time.monotonic()
    try:
        with pytest.raises(slots.LockBusy, match="kernel gate"):
            with slots.FileLock(path, "contender", timeout=0.15):
                pass
        assert time.monotonic() - started < 2
    finally:
        release.set()
    owner.join(5)
    assert owner.exitcode == 0


def test_interrupted_fd_wrapper_closes_marker_and_gate(tmp_path, monkeypatch):
    path = tmp_path / "wrapper.lock"
    lock = slots.FileLock(path, "interrupted wrapper")
    marker_descriptors = []

    def interrupted_wrapper(descriptor, *args, **kwargs):
        marker_descriptors.append(descriptor)
        raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(slots.os, "fdopen", interrupted_wrapper)
        with pytest.raises(KeyboardInterrupt):
            with lock:
                pass
    assert lock.gate_fd is None
    for descriptor in marker_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    # The deliberately interrupted write left an unreadable marker. Its normal
    # conservative recovery policy is unchanged; remove only our own fixture.
    path.unlink()
    with slots.FileLock(path, "after wrapper interruption"):
        pass
