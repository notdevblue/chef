"""Leaf Hermes workers with capabilities bound to a trusted Chef assignment.

File operations are descriptor-relative and never follow symlinks. Commands run
under the service account, not an OS sandbox; shared guards inspect supported
literal targets and protected-state fingerprints detect opaque state changes.
"""

import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from chef.guards import mission, questions
from chef.infrastructure.errors import UsageError
from chef.infrastructure.fingerprints import execution_digest as implementation_digest
from chef.infrastructure.process import child_env
from chef.runtimes.hermes import research, workers

READ = "chef_worker_read"
WRITE = "chef_worker_write"
SHELL = "chef_worker_shell"
READ_TOOLSET = "chef-worker-read"
WRITE_TOOLSET = "chef-worker-write"
SHELL_TOOLSET = "chef-worker-shell"
TOOLS = (READ, WRITE, SHELL)
DEVELOPERS = ("dev-small", "dev-medium")
OUTPUT_LIMIT = 64 * 1024
WRITE_LIMIT = 256 * 1024
ROLE_CONTEXT = {
    "researcher": "Inspect code and documentation read-only. Return conclusions with observed file locations.",
    "dev-small": "Implement a small, approved and fully specified change within the brief's file boundaries.",
    "dev-medium": "Implement the approved change across its assigned files and preserve external contracts.",
    "verifier": "Run only the checks and scenarios named in the brief. Return actual results, logs and unverified scope.",
    "code-reviewer": "Perform the first independent read-only review in a fresh session. Report actionable findings and verification gaps.",
    "reviewer-small": "Independently review the narrow revision and its directly affected callers read-only. Escalate expanded scope.",
}


def schema(name, description, properties, required):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


READ_SCHEMA = schema(READ, "Read bounded text, list a directory, or inspect Git HEAD in the assigned workspace.", {
    "operation": {"type": "string", "enum": ["read", "list", "head"]}, "path": {"type": "string"},
    "offset": {"type": "integer", "minimum": 0, "description": "Byte offset for read; use next_offset for continuation."}}, ["operation"])
WRITE_SCHEMA = schema(WRITE, "Write a text file or replace one exact unique text occurrence in the assigned workspace.", {
    "operation": {"type": "string", "enum": ["write", "patch"]}, "path": {"type": "string"},
    "content": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, ["operation", "path"])
SHELL_SCHEMA = schema(SHELL, "Run a foreground command at the assigned workspace root, with bounded output and timeout.", {
    "command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 300}}, ["command"])


def policy_failure(outcome):
    from chef.runtimes.hermes.plugin import failed_policy

    if outcome.deny:
        raise ValueError(outcome.deny)
    if failed_policy(outcome.messages):
        raise ValueError("worker guard policy could not be fully evaluated")


class NativeJob(research.ResearchJob):
    def __init__(self, cwd, state, active, checker, slot, role, context_path=None):
        if role not in workers.ROLES:
            raise ValueError("unsupported Chef role")
        if Path(cwd).is_symlink():
            raise ValueError("the assigned workspace cannot be a symlink")
        super().__init__(cwd, state, active, checker, slot)
        self.role = role
        self.context_path = context_path
        self.confirmed = threading.Event()
        self.process_lock = threading.Lock()
        self.process = None
        self.snapshot = workers.protected_state(state, active.id)

    def check(self):
        super().check()
        if not os.path.samestat(self.cwd.stat(), self.root_info):
            self.revoked.set()
            raise ValueError("the assigned workspace changed")
        if self.slot is not None:
            self.slot.check(self.active.session, self.active.id)
        if workers.protected_state(self.state, self.active.id) != self.snapshot:
            self.revoked.set()
            raise ValueError("protected mission state changed during worker; inspect it before accepting results")

    def protected(self, parts):
        # Configuration files are executable policy, not worker source targets.
        if super().protected(parts):
            return True
        folded = tuple(part.casefold() for part in parts)
        if len(folded) >= 2 and folded[0] in (".claude", ".codex", ".grok") and folded[1] == "agents":
            return False  # Briefs can refer to existing project role instructions.
        return any(part in (".hermes", ".claude", ".codex", ".grok", ".chef", ".han") for part in folded)

    def parts(self, path, writable=False):
        if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
            raise ValueError("use a relative path inside the assigned workspace")
        parts = tuple(path.split("/")) if path != "." else ()
        if any(part in ("", ".", "..") for part in parts) or self.protected(parts):
            raise ValueError("this path is unavailable to the worker")
        if writable and any(part.casefold() in (".hermes", ".claude", ".codex", ".grok", ".chef", ".han") for part in parts):
            raise ValueError("worker configuration writes are unavailable")
        return parts

    def payload(self, tool, inputs, task_id):
        return {"han_runtime": "hermes", "hook_event_name": "PreToolUse", "session_id": self.active.session,
                "task_id": task_id, "agent_id": "chef-hermes-worker:" + self.role, "cwd": str(self.cwd),
                "tool_name": tool, "tool_input": inputs}

    def guard(self, tool, inputs, task_id):
        self.check()
        if tool == "Bash" and self.slot is not None:
            self.slot.check_command(inputs["command"])
        payload = self.payload(tool, inputs, task_id)
        data = {"guard": self.checker, "cwd": str(self.cwd), "state": str(self.state),
                "session": self.active.session, "mission": self.active.id, "role": self.role, "backend": "hermes"}
        worker_payload = {**payload, "han_hermes_worker": data}
        denied = workers.worker_denial(worker_payload)
        if denied:
            raise ValueError(denied)
        paths = questions.tool_targets(payload)
        if tool == "Bash":
            literal, trees = questions.shell_targets(inputs["command"], tool, str(self.cwd), mission)
            paths.extend(literal + trees)
        for path in paths:
            target = Path(os.path.abspath(os.path.join(self.cwd, path)))
            if tool == READ and self.sources.contains(target):
                self.sources.bytes(target)
                continue
            if not target.is_relative_to(self.cwd):
                raise ValueError("worker target escaped the assigned workspace")
            self.parts(target.relative_to(self.cwd).as_posix(), writable=tool != READ)
            current = self.cwd
            for part in target.relative_to(self.cwd).parts:
                current /= part
                if current.is_symlink():
                    raise ValueError("worker paths cannot follow symlinks")
        # Run the shared policy without han_hermes_worker to avoid executing
        # the project command twice or losing its child-only context env.
        policy_failure(mission.decide(payload))
        env = child_env(str(self.cwd))
        if self.context_path:
            env[workers.WORKER_ENV] = str(self.context_path)
        denied = workers.project_denial(worker_payload, env=env)
        if denied:
            raise ValueError(denied)
        self.check()

    def call(self, tool, args, task_id):
        if not isinstance(task_id, str) or not task_id or not isinstance(args, dict):
            raise ValueError("trusted task identity and object arguments are required")
        with self.lock:
            self.check()
            if tool == READ:
                result = self.read(args, task_id)
            elif tool == WRITE and self.role in DEVELOPERS:
                result = self.write(args, task_id)
            elif tool == SHELL and self.role not in workers.READ_ONLY:
                result = self.shell(args, task_id)
            else:
                raise ValueError("this tool is unavailable to the assigned role")
            self.check()
            return result

    @contextmanager
    def directory(self, parts):
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(self.cwd, flags)
            stack.callback(os.close, fd)
            if not os.path.samestat(os.fstat(fd), self.root_info):
                raise ValueError("the assigned workspace changed")
            for part in parts:
                fd = os.open(part, flags, dir_fd=fd)
                stack.callback(os.close, fd)
            yield fd, flags, stack

    def read(self, args, task_id):
        if set(args) - {"operation", "path", "offset"} or args.get("operation") not in ("list", "read", "head"):
            raise ValueError("choose list, read or head and an optional relative path")
        operation = args["operation"]
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0 or offset > (1 << 63) - 1 or ("offset" in args and operation != "read"):
            raise ValueError("offset must be a nonnegative byte offset for read")
        path = args.get("path", ".")
        if operation == "read" and self.sources.contains(path):
            self.guard(READ, {"operation": operation, "path": path, "offset": offset}, task_id)
            return self.sources.read(path, offset)
        parts = self.parts(path)
        if operation == "head" and "path" in args:
            raise ValueError("head takes no path")
        if operation == "read" and not parts:
            raise ValueError("read requires a file path")
        self.guard(READ, {"operation": operation, "path": str(self.cwd.joinpath(*parts)), "offset": offset}, task_id)
        if operation == "read":
            with self.directory(parts[:-1]) as (directory, _, __):
                with os.fdopen(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory), "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("only regular text files can be read")
                    stream.seek(offset)
                    data = stream.read(research.READ_LIMIT + 1)
                if b"\x00" in data:
                    raise ValueError("only regular text files can be read")
                return {"text": data[:research.READ_LIMIT].decode("utf-8", "replace"),
                        "truncated": len(data) > research.READ_LIMIT, "next_offset": offset + min(len(data), research.READ_LIMIT)}
        with self.directory(()) as (root, flags, stack):
            return research.inspect_head(str(self.cwd)) if operation == "head" else self.read_path(operation, parts, root, flags, stack)

    def write(self, args, task_id):
        operation = args.get("operation")
        keys = {"operation", "path", "content"} if operation == "write" else {"operation", "path", "old_text", "new_text"}
        if set(args) != keys or operation not in ("write", "patch"):
            raise ValueError("write requires content; patch requires old_text and new_text")
        if any(not isinstance(args[key], str) for key in keys):
            raise ValueError("write and patch values must be strings")
        parts = self.parts(args["path"], writable=True)
        if not parts:
            raise ValueError("write requires a file path")
        inputs = {"file_path": str(self.cwd.joinpath(*parts))}
        if operation == "write":
            inputs["content"] = args["content"]
        else:
            inputs.update(old_string=args["old_text"], new_string=args["new_text"])
        self.guard("Write" if operation == "write" else "Edit", inputs, task_id)
        with self.directory(parts[:-1]) as (directory, _, __):
            name = parts[-1]
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise ValueError("write requires a regular file without aliases")
            if info and (not info.st_mode & 0o222 or not os.access(name, os.W_OK, dir_fd=directory)):
                raise ValueError("the existing file is read-only; follow the project's checkout workflow first")
            before = self.file_version(directory, name) if info else None
            content = args.get("content")
            if operation == "patch":
                if info is None or info.st_size > WRITE_LIMIT or not args["old_text"]:
                    raise ValueError("patch requires a bounded existing text file and nonempty old_text")
                with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory), "rb") as stream:
                    original = stream.read(WRITE_LIMIT + 1).decode("utf-8")
                if original.count(args["old_text"]) != 1:
                    raise ValueError("old_text must match exactly once")
                content = original.replace(args["old_text"], args["new_text"], 1)
            encoded = content.encode("utf-8")
            if len(encoded) > WRITE_LIMIT or b"\x00" in encoded:
                raise ValueError("write requires bounded UTF-8 text")
            temporary = ".chef-write-" + uuid.uuid4().hex
            try:
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      info.st_mode & 0o777 if info else 0o666, dir_fd=directory), "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.check()
                if info is None:
                    # Atomic create without replacing a concurrently created file.
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                else:
                    if self.file_version(directory, name) != before:
                        raise ValueError("the destination changed during the write; reread before editing")
                    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return {"path": args["path"], "bytes": len(encoded)}

    @staticmethod
    def file_version(directory, name):
        with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("write requires a regular file without aliases")
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                    hashlib.file_digest(stream, "sha256").hexdigest())

    def shell(self, args, task_id):
        command, timeout = args.get("command"), args.get("timeout", 60)
        if (set(args) - {"command", "timeout"} or not isinstance(command, str) or not command.strip()
                or len(command) > 32768 or "\x00" in command or type(timeout) is not int or not 1 <= timeout <= 300):
            raise ValueError("provide a bounded command and timeout of 1..300 seconds")
        self.guard("Bash", {"command": command}, task_id)
        deadline = time.monotonic() + timeout
        # Disk-backed capture bounds memory even for a noisy compiler. The
        # polling loop also caps the files and observes cancellation/ownership.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            with self.process_lock:
                self.check()
                process = subprocess.Popen(["/bin/sh", "-c", command], cwd=self.cwd, env=child_env(str(self.cwd)),
                                           stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                self.process = process
            try:
                while True:
                    self.check()
                    if max(os.fstat(stdout.fileno()).st_size, os.fstat(stderr.fileno()).st_size) > OUTPUT_LIMIT:
                        raise ValueError("worker command exceeded the output limit")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("worker command timed out")
                    try:
                        process.wait(timeout=min(.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                self.stop_process(process)
                with self.process_lock:
                    self.process = None
                # Opaque commands must never leave protected mutations accepted.
                self.check()
            stdout.seek(0)
            stderr.seek(0)
            out, err = stdout.read(OUTPUT_LIMIT + 1), stderr.read(OUTPUT_LIMIT + 1)
            return {"exit_code": process.returncode, "stdout": out[:OUTPUT_LIMIT].decode("utf-8", "replace"),
                    "stderr": err[:OUTPUT_LIMIT].decode("utf-8", "replace"),
                    "truncated": len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT}

    @staticmethod
    def stop_process(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def revoke(self):
        self.revoked.set()
        with self.process_lock:
            if self.process is not None:
                self.stop_process(self.process)

    def drain(self):
        with self.lock:
            pass


class NativeWorkers(research.ResearchWorkers):
    """Public lifecycle dispatch; identities come only from host lifecycle hooks."""

    def check_available(self):
        super().check_available()
        self.check_tool_execution()

    def check_tool_execution(self):
        if os.name != "posix" or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "killpg")):
            raise ValueError("native Hermes workers require macOS or Linux filesystem/process capabilities")

    @contextmanager
    def assignment(self, role, cwd, state, active, checker, slot, config=None):
        self.check_tool_execution()
        with tempfile.TemporaryDirectory(prefix="chef-native-worker-") as directory:
            context = Path(directory) / "context.json"
            job = NativeJob(cwd, state, active, checker, slot, role, context)
            context.write_text(json.dumps({"schema": 1, "pid": os.getpid(), "session": active.session,
                "cwd": str(job.cwd), "state": str(job.state), "mission": active.id, "role": role,
                "backend": "hermes", "guard": checker, "digest": implementation_digest()}), encoding="utf-8")
            context.chmod(0o600)
            try:
                job.check()
                yield job
            finally:
                job.revoke()
                job.drain()

    def started(self, **kwargs):
        with self.lock:
            if kwargs.get("child_session_id") in self.retired:
                return
        super().started(**kwargs)
        with self.lock:
            job = self.children.get(kwargs.get("child_session_id"))
            if job:
                job.confirmed.set()

    def stopped(self, *, parent_session_id=None, child_session_id=None, **_):
        with self.lock:
            job = self.children.get(child_session_id)
        if job and job.active.session == parent_session_id:
            job.revoke()
            with self.lock:
                self.children.pop(child_session_id, None)
                self.retired.add(child_session_id)

    def tool(self, tool, args, session_id="", task_id="", **_):
        with self.lock:
            job = self.children.get(session_id)
        try:
            if job is None:
                raise ValueError("only the assigned Hermes worker can use this tool")
            return json.dumps(job.call(tool, args, task_id), ensure_ascii=True)
        except (OSError, ValueError, UsageError, subprocess.SubprocessError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=True)

    def run(self, role, prompt, timeout, cwd, state, active, checker, slot, config=None):
        self.check_available()
        from chef.runtimes.hermes.contracts import native_routing, role_contract, settings_for

        config = native_routing(config)
        config, instructions = role_contract(settings_for(cwd), cwd, role, config)
        with self.assignment(role, cwd, state, active, checker, slot) as job:
            call = uuid.uuid4().hex
            goal = f"Chef {role} assignment {call}\n{prompt}"
            toolsets = [READ_TOOLSET] + ([WRITE_TOOLSET] if role in DEVELOPERS else [])
            if role not in workers.READ_ONLY:
                toolsets.append(SHELL_TOOLSET)
            context = (ROLE_CONTEXT[role] + " Read AGENTS.md and the project workflow, then follow the self-contained brief's "
                       "scope, file ownership, approved contracts and verification requirements. Return missing prerequisites "
                       "or design decisions to the coordinator. Preserve other contributors' changes. "
                       "You are a fresh Chef leaf worker. Use only chef_worker_read, chef_worker_write and "
                       "chef_worker_shell as permitted by this role. Paths are relative to the assigned workspace; "
                       "commands run at its root. Do not delegate, change mission state/ownership or configuration. "
                       "Return observed evidence and unverified work. Assigned workspace: " + str(job.cwd)
                       + ("\nProject role instructions:\n" + instructions if instructions else ""))
            with self.lock:
                self.pending[goal] = job
            handle = None
            completed = False
            try:
                handle = self.lifecycle.launch({"goal": goal, "role": "leaf", "allowed_toolsets": toolsets,
                    "parent_session_id": active.session, "correlation_id": call, "context": context,
                    "metadata": {"chef_role": role, "backend": "hermes"}, **config})
                if not job.confirmed.wait(min(timeout, 5)) or job.child_id != research.field(handle, "subagent_id"):
                    raise ValueError("Hermes did not confirm the worker's session identity")
                if research.field(handle, "role") != "leaf":
                    raise ValueError("Hermes did not launch the worker as a leaf")
                if config.get("model") and research.field(handle, "model") != config["model"]:
                    raise ValueError("Hermes did not apply the required role model")
                terminal = self.lifecycle.wait(handle, timeout_seconds=timeout)
                completed = research.field(terminal, "completed") is True
                if not completed:
                    raise ValueError("Hermes worker timed out or did not confirm completion")
                result = self.lifecycle.result(handle)
                if research.field(result, "ready") is not True or mission.read_active(state) != active:
                    raise ValueError("the worker result or mission ownership could not be confirmed")
                if workers.protected_state(state, active.id) != job.snapshot:
                    raise ValueError("protected mission state changed during worker; inspect it before accepting results")
                if slot is not None:
                    slot.check(active.session, active.id)
                return {"ok": research.field(result, "terminal_state") == "SUCCEEDED", "runtime": "hermes",
                    "role": "chef:" + role, "result": research.field(result, "summary") or "",
                    "error": research.field(result, "error_message") or "", "model": research.field(handle, "model"),
                    "provider": research.field(handle, "provider")}
            finally:
                job.revoke()
                try:
                    if handle is not None and not completed:
                        self.lifecycle.cancel(handle, reason="Chef assignment ended before completion")
                finally:
                    job.drain()
                    with self.lock:
                        self.pending.pop(goal, None)
                        self.children.pop(job.child_session, None)
                        if job.child_session:
                            self.retired.add(job.child_session)
    def __init__(self, lifecycle):
        super().__init__(lifecycle)
        self.retired = set()

    def owns(self, session_id):
        with self.lock:
            return session_id in self.children or session_id in self.retired
