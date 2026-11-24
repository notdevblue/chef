"""Explicit, trusted model bridges using Chef's guarded worker tools.

The bridge is administrator-selected executable code, not an OS sandbox. Stock
agent CLIs are not this protocol: a bridge must request all workspace tools over
JSONL instead of starting its own write-capable agent tool loop.
"""

import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

from chef.infrastructure.process import child_env

LIMIT = 1024 * 1024
OUTPUT_LIMIT = 8 * LIMIT
TOOLS = ("chef_worker_read", "chef_worker_write", "chef_worker_shell")


def attachment_settings(settings, name):
    attachments = settings.get("attachments", {})
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
            or name in ("codex", "hermes", "claude") or not isinstance(attachments, dict) or name not in attachments):
        raise ValueError("workers.backend must be hermes, codex, claude or an explicitly configured attachment name")
    config = attachments[name]
    command = config.get("command") if isinstance(config, dict) else None
    if (not isinstance(config, dict) or set(config) != {"command"} or not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg or any(c in arg for c in "\x00\r\n") for arg in command)
            or not os.path.isabs(command[0])):
        raise ValueError("worker attachment requires command: an argv array with an absolute executable")
    return list(command)


class Connection:
    """Bounded JSONL exchange with one process and a single overall deadline."""

    def __init__(self, command, cwd, deadline):
        env = child_env(cwd)
        env.pop("CHEF_HERMES_WORKER", None)
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != "nt")
        self.deadline = deadline
        self.events = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.failure = None
        self.writers = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def remaining(self):
        if self.failure is not None:
            raise self.failure
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("worker attachment timed out")
        return value

    def _read(self):
        count = 0
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(LIMIT + 1)
                count += len(line)
                if not line:
                    value = ValueError("worker attachment exited before a complete response")
                elif count > OUTPUT_LIMIT:
                    value = ValueError("worker attachment exceeded its total stdout output limit")
                elif len(line) > LIMIT or not line.endswith(b"\n"):
                    value = ValueError("worker attachment response exceeds the JSONL size limit")
                else:
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        value = ValueError("worker attachment response is not JSON")
                try:
                    self.events.put(value, timeout=0.1)
                except queue.Full:
                    # A bridge flooding stdout cannot grow unbounded memory or
                    # block shutdown. A full queue itself invalidates the call.
                    self.failure = ValueError("worker attachment exceeded the response queue limit")
                    self.closed.set()
                if isinstance(value, BaseException):
                    break
        except (OSError, ValueError) as exc:
            try:
                self.events.put_nowait(exc)
            except queue.Full:
                pass

    def _read_errors(self):
        count = 0
        try:
            while not self.closed.is_set():
                chunk = self.process.stderr.read(8192)
                if not chunk:
                    return
                count += len(chunk)
                if count > LIMIT:
                    self.failure = ValueError("worker attachment exceeded its stderr output limit")
                    self.closed.set()
                    try:
                        self.events.put_nowait(self.failure)
                    except queue.Full:
                        pass
                    return
        except (OSError, ValueError):
            pass

    def send(self, value):
        data = (json.dumps(value, ensure_ascii=True) + "\n").encode("utf-8")
        if len(data) > LIMIT:
            raise ValueError("worker attachment request exceeds the JSONL size limit")
        done = queue.Queue(maxsize=1)

        def write():
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                done.put(None)
            except (OSError, ValueError) as exc:
                done.put(exc)

        writer = threading.Thread(target=write, daemon=True)
        self.writers.append(writer)
        writer.start()
        try:
            error = done.get(timeout=self.remaining())
        except queue.Empty:
            raise TimeoutError("worker attachment timed out writing a request") from None
        if error:
            raise ValueError("worker attachment stopped accepting requests") from error

    def receive(self):
        if self.closed.is_set():
            raise self.failure or ValueError("worker attachment connection is closed")
        try:
            value = self.events.get(timeout=self.remaining())
        except queue.Empty:
            raise TimeoutError("worker attachment timed out waiting for a response") from None
        if isinstance(value, BaseException):
            raise value
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError("worker attachment requires schema 1 JSON objects")
        return value

    def stop(self):
        self.closed.set()
        # Always terminate the group, even after a terminal result or leader
        # exit: descendants must not retain access after the assignment ends.
        def kill(hard):
            try:
                if os.name == "posix":
                    os.killpg(self.process.pid, signal.SIGKILL if hard else signal.SIGTERM)
                elif hard:
                    self.process.kill()
                else:
                    self.process.terminate()
            except OSError:
                pass

        kill(False)
        try:
            self.process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            pass
        finally:
            kill(True)
            self.process.wait(timeout=2)
        self.reader.join(timeout=0.2)
        self.error_reader.join(timeout=0.2)
        for writer in self.writers:
            writer.join(timeout=0.2)
        # Reader/writer threads may hold a pipe lock until a descendant exits.
        # Do not block assignment cancellation on an escaped descendant's pipe.
        if not self.reader.is_alive():
            self.close_pipe(self.process.stdout)
        if not self.error_reader.is_alive():
            self.close_pipe(self.process.stderr)
        if not any(writer.is_alive() for writer in self.writers):
            self.close_pipe(self.process.stdin)

    @staticmethod
    def close_pipe(stream):
        try:
            stream.close()
        except (OSError, ValueError):
            pass  # A killed bridge can leave a buffered write with EPIPE/EINVAL.


@contextmanager
def connection(command, timeout):
    # The bridge receives the assigned path in JSON; its process cwd is a
    # private transport directory, never an implicitly selected workspace.
    with tempfile.TemporaryDirectory(prefix="chef-attachment-") as cwd:
        link = Connection(command, cwd, time.monotonic() + timeout)
        try:
            yield link
        finally:
            link.stop()


def advertised_roles(link):
    from chef.runtimes.hermes.workers import ROLES

    link.send({"schema": 1, "action": "status"})
    response = link.receive()
    roles = response.get("roles")
    if (set(response) != {"schema", "type", "transport", "roles"} or response.get("type") != "status"
            or response.get("transport") != "chef-tools-v1" or not isinstance(roles, list) or not roles
            or any(role not in ["chef:" + value for value in ROLES] for role in roles)
            or len(set(roles)) != len(roles)):
        raise ValueError("worker attachment must advertise Chef roles and chef-tools-v1 transport")
    return roles


class Attachment:
    def __init__(self, settings, name):
        self.name = name
        self.command = attachment_settings(settings, name)

    def status(self):
        with connection(self.command, 10) as link:
            return advertised_roles(link)

    def run(self, native, role, prompt, timeout, cwd, state, active, checker, slot):
        # Session/task authority is host-created. Protocol IDs correlate messages
        # only and can never claim a native parent/child identity.
        task = "chef-attachment-" + uuid.uuid4().hex
        with native.assignment(role, cwd, state, active, checker, slot) as job:
            with connection(self.command, timeout) as link:
                completed = False
                try:
                    if "chef:" + role not in advertised_roles(link):
                        raise ValueError("selected attachment does not support this Chef role")
                    job.check()
                    link.send({"schema": 1, "action": "delegate", "id": task, "role": "chef:" + role,
                               "prompt": prompt, "workspace": cwd, "timeout": timeout,
                               "tools": list(TOOLS) if role in ("dev-small", "dev-medium") else
                               [TOOLS[0], TOOLS[2]] if role == "verifier" else [TOOLS[0]]})
                    seen = set()
                    while True:
                        response = link.receive()
                        job.check()
                        if response.get("type") == "result":
                            if (set(response) != {"schema", "type", "ok", "result", "error"}
                                    or type(response.get("ok")) is not bool
                                    or not all(isinstance(response.get(key), str) for key in ("result", "error"))):
                                raise ValueError("invalid worker attachment terminal result")
                            completed = True
                            return {"ok": response["ok"], "runtime": self.name, "role": "chef:" + role,
                                    "result": response["result"], "error": response["error"]}
                        request = response.get("id")
                        if (set(response) != {"schema", "type", "id", "tool", "arguments"}
                                or response.get("type") != "tool" or not isinstance(request, str)
                                or not request or len(request) > 128 or request in seen or len(seen) >= 1000
                                or response.get("tool") not in TOOLS or not isinstance(response.get("arguments"), dict)):
                            raise ValueError("invalid worker attachment tool request")
                        seen.add(request)
                        args = dict(response["arguments"])
                        if response["tool"] == "chef_worker_shell":
                            requested = args.get("timeout", 60)
                            if type(requested) is not int or not 1 <= requested <= 3600:
                                raise ValueError("invalid worker attachment shell timeout")
                            remaining = int(link.remaining())
                            if not remaining:
                                raise TimeoutError("worker attachment tool deadline elapsed")
                            args["timeout"] = min(requested, remaining, 300)
                        try:
                            result = job.call(response["tool"], args, task)
                            output = {"ok": True, "result": result}
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                            output = {"ok": False, "error": str(exc)}
                        link.remaining()
                        link.send({"schema": 1, "type": "tool_result", "id": request, **output})
                finally:
                    job.revoke()
                    if not completed:
                        # Revoke host tool authority before giving the bridge a
                        # best-effort cancellation notice, then kill its group.
                        link.deadline = time.monotonic() + 0.2
                        try:
                            link.send({"schema": 1, "action": "cancel", "id": task})
                        except (OSError, ValueError, TimeoutError):
                            pass
