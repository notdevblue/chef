"""infrastructure / locking."""

import errno
import os
import threading
import time
from datetime import date, datetime

from chef.infrastructure.paths import normalize_path

LOCK_UNREADABLE_SECONDS = 10


LOCK_POLL_SECONDS = 0.2


class LockBusy(RuntimeError):
    pass


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        code = wintypes.DWORD()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


THREAD_LOCKS = {}


THREAD_LOCKS_GUARD = threading.Lock()


def thread_lock_of(path):
    key = normalize_path(path)
    with THREAD_LOCKS_GUARD:
        return THREAD_LOCKS.setdefault(key, threading.Lock())


class FileLock:
    """PID marker protected by a permanent kernel-locked ``.gate`` sidecar.

    The gate must never be unlinked: replacing its inode would split callers
    across independent locks. The kernel releases it when its owner exits.
    Use the context manager; acquire() is an internal PID-marker operation.
    """

    def __init__(self, path, label, timeout=0):
        self.path = os.path.realpath(os.path.abspath(os.fspath(path)))
        self.label = label
        self.timeout = timeout
        self.holder = ""
        self.thread_lock = thread_lock_of(self.path)
        self.gate_fd = None
        self.deadline = 0

    def __enter__(self):
        self.deadline = time.monotonic() + self.timeout
        if not self.thread_lock.acquire(timeout=self.timeout):
            raise LockBusy(f"{self.label} 잠금 사용 중 (같은 프로세스의 다른 작업)")
        try:
            self.acquire_gate()
            self.acquire()
        except BaseException:
            try:
                self.release_gate()
            finally:
                self.thread_lock.release()
            raise
        return self

    def __exit__(self, *_):
        try:
            try:
                if self.read_pid() == os.getpid():
                    os.remove(self.path)
            except OSError:
                pass
        finally:
            try:
                self.release_gate()
            finally:
                self.thread_lock.release()

    def acquire_gate(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.gate_fd = os.open(self.path + ".gate", os.O_CREAT | os.O_RDWR, 0o600)
        # Byte-range locks can extend beyond EOF. Initializing a byte before
        # locking races with another process already holding that byte on Windows.
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(self.gate_fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.gate_fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise LockBusy(f"{self.label} 잠금 사용 중 (kernel gate)") from error
                time.sleep(min(LOCK_POLL_SECONDS, remaining))

    def release_gate(self):
        # Closing the descriptor releases flock/byte locks, including failed
        # acquisitions. Keep the sidecar itself for the next caller.
        if self.gate_fd is not None:
            descriptor, self.gate_fd = self.gate_fd, None
            os.close(descriptor)

    def acquire(self):
        if self.gate_fd is None:
            raise RuntimeError("FileLock.acquire requires the context manager kernel gate")
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self.reclaim_if_stale():
                    continue
                if time.monotonic() >= self.deadline:
                    raise LockBusy(f"{self.label} 잠금 사용 중 ({self.holder})")
                time.sleep(LOCK_POLL_SECONDS)
                continue
            try:
                marker = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            except BaseException:
                os.close(fd)
                raise
            with marker as f:
                f.write(f"{os.getpid()}\t{datetime.now().isoformat(timespec='seconds')}\n")
            return

    def read_holder(self):
        with open(self.path, encoding="utf-8", errors="replace") as f:
            parts = f.read().strip().split("\t")
        pid = int(parts[0]) if parts[0].isdigit() else 0
        return pid, (parts[1] if len(parts) > 1 else "")

    def read_pid(self):
        try:
            return self.read_holder()[0]
        except OSError:
            return 0

    def reclaim_if_stale(self):
        try:
            pid, started = self.read_holder()
            age = time.time() - os.path.getmtime(self.path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        self.holder = f"pid {pid}, {started or '시각 없음'}"
        # Age alone cannot revoke a live process's critical section.
        # Keep recovery of dead/self leftovers and old unreadable files.
        stale = (
            pid == os.getpid()
            or (pid and not process_alive(pid))
            or (not pid and age > LOCK_UNREADABLE_SECONDS)
        )
        if not stale:
            return False
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
