"""Read-only Chef workers using the public Hermes subagent lifecycle."""

import json
import os
import stat
import subprocess
import threading
import uuid
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from chef.guards import mission
from chef.infrastructure.errors import UsageError
from chef.infrastructure.paths import same_path
from chef.runtimes.hermes.workers import project_denial

TOOL = "chef_research_read"
TOOLSET = "chef-research"
READ_LIMIT = 64 * 1024
LIST_LIMIT = 200
SCHEMA = {
    "name": TOOL, "description": "Inspect the workspace assigned to this Chef researcher.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["list", "read", "head"]},
        "path": {"type": "string", "description": "Workspace-relative path; '.' lists the root. Omit for head."},
    }, "required": ["operation"], "additionalProperties": False},
}


def field(record, name):
    """The public Hermes API uses objects in-process and mappings over RPC."""
    return record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)


def profile_config():
    from hermes_cli.config import load_config_readonly

    return load_config_readonly()


def inspect_head(cwd):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", "rev-parse", "--show-toplevel", "--verify", "HEAD"], cwd=cwd,
                            env=env, capture_output=True, text=True, timeout=5)
    lines = result.stdout.splitlines()
    if result.returncode or len(lines) != 2 or not same_path(lines[0], cwd):
        raise ValueError("the assigned workspace has no Git HEAD")
    return {"head": lines[1]}


class ResearchJob:
    def __init__(self, cwd, state, active, checker, slot):
        self.cwd = Path(cwd).resolve(strict=True)
        self.state = Path(state)
        self.active = active
        self.checker = checker
        self.slot = slot
        self.root_info = self.cwd.stat()
        self.lock = threading.Lock()
        self.revoked = threading.Event()
        self.child_session = None
        self.child_id = None
        from chef.runtimes.hermes.contracts import ReadSources, settings_for

        self.sources = ReadSources(settings_for(self.cwd).get("readOnlySources"))
        for path in self.sources.files:
            source = Path(path)
            if source.is_relative_to(self.cwd) or source.is_relative_to(self.state.resolve()):
                raise ValueError("readOnlySources must name external references, never workspace or mission state files")

    def check(self):
        if self.revoked.is_set() or mission.read_active(str(self.state)) != self.active:
            raise ValueError("the Chef research assignment is no longer active")

    def protected(self, parts):
        folded = tuple(part.casefold() for part in parts)
        if ".git" in folded:
            return True
        if not self.state.is_relative_to(self.cwd):
            return False
        state = tuple(part.casefold() for part in self.state.relative_to(self.cwd).parts)
        return folded[:len(state)] == state

    def inspect(self, args, task_id):
        if not isinstance(args, dict) or set(args) - {"operation", "path"}:
            raise ValueError("provide an operation and an optional relative path")
        operation = args.get("operation")
        path = args.get("path", ".")
        if operation not in ("list", "read", "head") or not isinstance(path, str):
            raise ValueError("choose list, read or head")
        if operation == "head" and "path" in args:
            raise ValueError("head takes no path")
        if operation == "read" and self.sources.contains(path):
            with self.lock:
                self.check()
                if self.slot is not None:
                    self.slot.check(self.active.session, self.active.id)
                denied = project_denial({
                    "han_runtime": "hermes", "session_id": self.active.session, "cwd": str(self.cwd),
                    "task_id": task_id, "tool_name": TOOL, "tool_input": {"operation": operation, "path": path},
                    "han_hermes_worker": {"guard": self.checker},
                })
                if denied:
                    raise ValueError(denied)
                result = self.sources.read(path)
                self.check()
                return result
        parts = tuple(path.split("/")) if path != "." else ()
        if any(part in ("", ".", "..") for part in parts) or "\x00" in path or "\\" in path:
            raise ValueError("use a relative path inside the assigned workspace")
        if self.protected(parts) or (operation == "read" and not parts):
            raise ValueError("this path is unavailable to the research worker")
        with self.lock, ExitStack() as stack:
            self.check()
            if self.slot is not None:
                self.slot.check(self.active.session, self.active.id)
            denied = project_denial({
                "han_runtime": "hermes", "hook_event_name": "PreToolUse", "session_id": self.active.session,
                "task_id": task_id, "agent_id": "chef-hermes-researcher", "cwd": str(self.cwd),
                "tool_name": TOOL, "tool_input": {"operation": operation, "path": str(self.cwd.joinpath(*parts))},
                "han_hermes_worker": {"guard": self.checker},
            })
            if denied:
                raise ValueError(denied)
            self.check()
            dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root = os.open(self.cwd, dir_flags)
            stack.callback(os.close, root)
            if not os.path.samestat(os.fstat(root), self.root_info):
                raise ValueError("the assigned workspace changed")
            result = inspect_head(str(self.cwd)) if operation == "head" else self.read_path(operation, parts, root, dir_flags, stack)
            self.check()
            return result

    def read_path(self, operation, parts, directory, dir_flags, stack):
        directories = parts if operation == "list" else parts[:-1]
        for name in directories:
            directory = os.open(name, dir_flags, dir_fd=directory)
            stack.callback(os.close, directory)
        if operation == "list":
            entries = []
            with os.scandir(directory) as listing:
                for entry in listing:
                    if self.protected((*parts, entry.name)) or entry.is_symlink():
                        continue
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if is_directory or entry.is_file(follow_symlinks=False):
                        entries.append(entry.name + ("/" if is_directory else ""))
                    if len(entries) > LIST_LIMIT:
                        break
            return {"entries": sorted(entries[:LIST_LIMIT]), "truncated": len(entries) > LIST_LIMIT}
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        name = parts[-1]
        if not stat.S_ISREG(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise ValueError("only regular text files can be read")
        with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("only regular text files can be read")
            data = stream.read(READ_LIMIT + 1)
        if b"\x00" in data:
            raise ValueError("only regular text files can be read")
        return {"text": data[:READ_LIMIT].decode("utf-8", errors="replace"), "truncated": len(data) > READ_LIMIT}


class ResearchWorkers:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.lock = threading.Lock()
        self.pending = {}
        self.children = {}

    def check_available(self):
        if not all(callable(getattr(self.lifecycle, method, None)) for method in ("launch", "wait", "result", "cancel")):
            raise ValueError("Hermes public subagent lifecycle is unavailable in this plugin host")
        delegation = profile_config().get("delegation", {})
        if not isinstance(delegation, dict) or delegation.get("inherit_mcp_toolsets") is not False:
            raise ValueError("set delegation.inherit_mcp_toolsets: false in the Hermes profile for research workers")
        if delegation.get("orchestrator_enabled") is not False and delegation.get("max_spawn_depth", 1) not in (1, "1"):
            raise ValueError("Hermes research workers require leaf delegation; set delegation.max_spawn_depth: 1")

    def started(self, *, parent_session_id=None, child_session_id=None, child_subagent_id=None, child_goal=None, **_):
        with self.lock:
            job = self.pending.get(child_goal)
            if (job and job.active.session == parent_session_id and child_session_id and child_subagent_id
                    and child_session_id != parent_session_id and not job.child_session and child_session_id not in self.children):
                job.child_session, job.child_id = child_session_id, child_subagent_id
                self.children[child_session_id] = job

    def stopped(self, *, parent_session_id=None, child_session_id=None, **_):
        with self.lock:
            job = self.children.get(child_session_id)
            if job and job.active.session == parent_session_id:
                job.revoked.set()
                del self.children[child_session_id]

    def owns(self, session_id):
        with self.lock:
            return session_id in self.children

    def read(self, args, session_id="", task_id="", **_):
        with self.lock:
            job = self.children.get(session_id)
        try:
            if job is None or not task_id:
                raise ValueError("only the assigned Hermes research worker can use this tool")
            return json.dumps(job.inspect(args, task_id), ensure_ascii=True)
        except (OSError, ValueError, UsageError, subprocess.SubprocessError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=True)

    def run(self, prompt, timeout, cwd, state, active, checker, slot):
        self.check_available()
        job = ResearchJob(cwd, state, active, checker, slot)
        # The host reports the child session via subagent_start. The unique
        # goal correlates that event to this launch, even for concurrent tasks.
        call = uuid.uuid4().hex
        goal = f"Chef research assignment {call}\n{prompt}"
        with self.lock:
            self.pending[goal] = job
        handle = None
        completed = False
        try:
            job.check()
            handle = self.lifecycle.launch({
                "goal": goal, "role": "leaf", "allowed_toolsets": [TOOLSET],
                "parent_session_id": active.session, "correlation_id": call,
                "context": ("You are the Chef researcher. Use chef_research_read to inspect the assigned workspace. "
                            "Cite observed paths and results. Report any checks you could not run."),
            })
            if not job.child_session or job.child_id != field(handle, "subagent_id"):
                raise ValueError("Hermes did not confirm the research worker's session identity")
            if field(handle, "role") != "leaf":
                raise ValueError("Hermes did not launch the research worker as a leaf")
            terminal = self.lifecycle.wait(handle, timeout_seconds=timeout)
            completed = field(terminal, "completed") is True
            if not completed:
                raise ValueError("Hermes research worker timed out or did not confirm completion")
            result = self.lifecycle.result(handle)
            if field(result, "ready") is not True or mission.read_active(state) != active:
                raise ValueError("the worker result or mission ownership could not be confirmed")
            if slot is not None:
                slot.check(active.session, active.id)
            ok = field(result, "terminal_state") == "SUCCEEDED"
            return {"ok": ok, "runtime": "hermes", "role": "chef:researcher", "result": field(result, "summary") or "",
                    "error": field(result, "error_message") or "", "model": field(handle, "model"),
                    "provider": field(handle, "provider")}
        finally:
            job.revoked.set()
            with self.lock:
                self.pending.pop(goal, None)
                if completed or handle is None:
                    self.children.pop(job.child_session, None)
            try:
                if handle is not None and not completed:
                    self.lifecycle.cancel(handle, reason="Chef research assignment ended before completion")
            finally:
                with job.lock:
                    pass  # Drain any read that started before revocation.
