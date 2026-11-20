"""slots / execution."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from chef.infrastructure.locking import FileLock, LockBusy
from chef.infrastructure.process import child_env
from chef.slots.models import StepResult
from chef.slots.storage import Rows

EXCLUSIVE_LOCK_TIMEOUT = 30 * 60


def windows_executable(command, search_path):
    # CreateProcess does not use the child environment to find its executable.
    if isinstance(command, str):
        match = re.match(r'^\s*(?:"([^"]+)"|(\S+))', command)
        name = (match[1] or match[2]) if match else ""
    else:
        name = command[0]
    if name and not os.path.dirname(name):
        return shutil.which(name, path=search_path)
    return None


class Runner:
    def __init__(self, config, namespace=""):
        self.config = config
        self.namespace = namespace
        self.rows = Rows(config.rows_path)
        self.say_lock = threading.Lock()
        self.timing_lock = threading.Lock()

    def say(self, text):
        with self.say_lock:
            print(text, file=sys.stderr, flush=True)

    def execute(self, step):
        os.makedirs(os.path.dirname(step.out), exist_ok=True)
        argv = step.command
        if isinstance(argv, str) and os.name != "nt":
            argv = shlex.split(argv)
        environment = child_env(step.cwd, self.config.vcs.executable)
        executable = None
        if os.name == "nt" and environment["PATH"] != os.environ.get("PATH"):
            executable = windows_executable(argv, environment["PATH"])
        with open(step.out, "wb") as out:
            try:
                return subprocess.run(
                    argv,
                    cwd=step.cwd or None,
                    env=environment,
                    executable=executable,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                ).returncode
            except OSError as e:
                out.write(f"실행 실패: {e}\n".encode("utf-8"))
                return 127

    def record_timing(self, name, step_id, started, seconds, code):
        path = self.config.timing_path(name, self.namespace)
        with self.timing_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"{started}\t{step_id}\t{seconds:.1f}\t{code}\n")

    def run_step(self, name, step):
        def timed():
            started = datetime.now().isoformat(timespec="seconds")
            begin = time.monotonic()
            code = self.execute(step)
            return started, time.monotonic() - begin, code

        try:
            if step.exclusive:
                with FileLock(self.config.exclusive_lock_path, "exclusive 단계", EXCLUSIVE_LOCK_TIMEOUT):
                    started, seconds, code = timed()
            else:
                started, seconds, code = timed()
        except LockBusy as e:
            os.makedirs(os.path.dirname(step.out), exist_ok=True)
            with open(step.out, "w", encoding="utf-8") as f:
                f.write(f"{e}\n")
            started, seconds, code = datetime.now().isoformat(timespec="seconds"), 0.0, 126
        self.record_timing(name, step.id, started, seconds, code)
        result = StepResult(step.id, code, seconds, code in step.ok_exit, step.out, step.log)
        self.say(f"[{name}] {step.id} {'ok' if result.ok else 'FAIL'} {seconds:.1f}s")
        return result

    def run_steps(self, name, steps):
        done = []
        for group in sorted({step.group for step in steps}):
            batch = [step for step in steps if step.group == group]
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                results = list(pool.map(lambda step: self.run_step(name, step), batch))
            done.extend(results)
            if not all(result.ok for result in results):
                break
        return done
