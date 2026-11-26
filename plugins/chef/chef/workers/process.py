"""Bounded CLI execution with cancellation and owned process-tree cleanup."""

import os
import signal
import shutil
import subprocess
import threading
import time
from pathlib import Path

MAX_OUTPUT = 4 * 1024 * 1024


class Cancelled(RuntimeError):
    pass


class WindowsJob:
    """Assign a suspended child before it can create untracked descendants."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("perProcess", ctypes.c_int64), ("perJob", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("readOps", "writeOps", "otherOps", "readBytes", "writeBytes", "otherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("limits", Limits), ("io", Counters), ("processMemory", ctypes.c_size_t),
                        ("jobMemory", ctypes.c_size_t), ("peakProcess", ctypes.c_size_t),
                        ("peakJob", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "cannot create worker job")
        value = Extended()
        value.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(value), ctypes.sizeof(value))
                or not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle))):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "cannot contain worker process tree")
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        if ntdll.NtResumeProcess(int(process._handle)) != 0:
            self.close()
            raise OSError("cannot resume contained worker")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run(argv, prompt, *, cwd, env, timeout, cancelled=None, on_start=None, complete=None):
    """Return capped UTF-8 output. Timeout/cancellation always terminate descendants."""
    if timeout <= 0:
        raise ValueError("worker timeout must be positive")
    argv = literal_command(argv)
    options = {"creationflags": 0x08000004} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    job = None
    threads = []
    output = [bytearray(), bytearray()]
    truncated = threading.Event()

    def drain(stream, index):
        while chunk := stream.read1(8192):
            room = MAX_OUTPUT - len(output[index])
            output[index].extend(chunk[:max(0, room)])
            if len(chunk) > room:
                truncated.set()

    def feed():
        try:
            process.stdin.write(prompt.encode("utf-8"))
            process.stdin.flush()
            if complete is None:
                process.stdin.close()
        except (OSError, ValueError):
            pass

    def stop():
        if job is not None:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()

    try:
        if os.name == "nt":
            job = WindowsJob(process)
        if on_start:
            on_start(process.pid)
        for index, stream in enumerate((process.stdout, process.stderr)):
            thread = threading.Thread(target=drain, args=(stream, index), daemon=True)
            thread.start()
            threads.append(thread)
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        threads.append(writer)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if complete and complete(bytes(output[0])):
                break
            if cancelled and cancelled():
                raise Cancelled("worker cancelled or ownership lost")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        stop()
        process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("worker descendants retained output pipes")
        if truncated.is_set():
            raise RuntimeError("worker output exceeded size limit")
        return subprocess.CompletedProcess(argv, process.returncode,
            output[0].decode("utf-8", "replace"), output[1].decode("utf-8", "replace"))
    finally:
        stop()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def literal_command(argv):
    """Bypass the npm cmd shim so role text never becomes cmd.exe syntax."""
    executable = Path(argv[0])
    if os.name != "nt" or executable.suffix.lower() not in (".cmd", ".bat", ".ps1"):
        return argv
    script = executable.parent / "node_modules/@openai/codex/bin/codex.js"
    node = executable.parent / "node.exe"
    if not node.is_file():
        resolved = shutil.which("node")
        node = Path(resolved) if resolved else node
    if (executable.stem.lower() == "codex" and node.is_absolute() and node.is_file()
            and node.suffix.lower() == ".exe" and script.is_file()):
        return [str(node), str(script), *argv[1:]]
    raise ValueError("worker CLI shell shim requires a directly executable installation")
