"""Hermes native plugin boundary, using trusted per-call session/task context.

The host catches callback exceptions and proceeds. Policy callbacks therefore
return explicit block directives on every internal failure instead of raising.
No process-global working directory or session environment is changed.
"""

import json
import os
import re
import sys
import threading
from contextlib import nullcontext

from chef.cli import mission_entry
from chef.guards import integrity, mission, questions, subagents
from chef.infrastructure.errors import UsageError
from chef.infrastructure.paths import inside_real_root, same_path
from chef.locations import PLUGIN_ROOT
from chef.runtimes.hermes import external_slots
from chef.runtimes.hermes import research as research_worker
from chef.runtimes.hermes import native as native_worker

SKILLS = ("mission", "slots", "adapter-init", "doctor", "distill")
PATCH_HEADER = re.compile(r"^(\*\*\* (?:Add File|Update File|Delete File|Move to): )(.+)$", re.MULTILINE)
ERROR_ARGUMENT = "_chef_bridge_error"


def block(message):
    return {"action": "block", "message": "Chef: " + message}


def identity(session_id, task_id):
    if not isinstance(session_id, str) or not session_id or any(c in session_id for c in "\x00\t\r\n"):
        raise ValueError("a trusted Hermes session ID is required")
    if not isinstance(task_id, str) or not task_id or any(c in task_id for c in "\x00\t\r\n"):
        raise ValueError("a trusted Hermes task ID is required")
    return session_id, task_id


def native_cwd(task_id):
    try:
        from tools.file_tools_paths import _resolve_path_for_task, _terminal_env_type_for_task
    except ModuleNotFoundError as exc:
        if exc.name != "tools.file_tools_paths":
            raise
        # Older Hermes releases defined these helpers in the file tool itself.
        from tools.file_tools import _resolve_path_for_task, _terminal_env_type_for_task

    if _terminal_env_type_for_task(task_id) != "local":
        raise ValueError("Chef Hermes requires a local terminal backend")
    return str(_resolve_path_for_task(".", task_id))


def bind_native_cwd(task_id, cwd):
    from tools.terminal_tool import register_task_env_overrides, resolve_task_overrides

    register_task_env_overrides(task_id, {**resolve_task_overrides(task_id), "cwd": cwd})


def absolute_path(value, cwd):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("a valid path is required")
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(value)))


def canonical_payload(tool_name, args, session_id, task_id, cwd):
    """Map full native inputs to the shared guards, without model identities."""
    if not isinstance(args, dict):
        raise ValueError("tool arguments must be an object")
    # Native tools do not execute these model-supplied context keys. Letting
    # them reach shared normalization would inspect a different directory or
    # shell from the one the native tool actually uses.
    values = {key: value for key, value in args.items() if key not in (
        "cwd", "session_id", "agent_id", "agent_type", "han_runtime", "shell",
    )}
    tool = tool_name
    if tool_name == "terminal":
        tool = "Bash"
        if not isinstance(values.get("command"), str) or not values["command"]:
            raise ValueError("a terminal command is required")
    elif tool_name == "write_file":
        tool = "Write"
        values["file_path"] = absolute_path(args.get("path"), cwd)
    elif tool_name == "patch":
        if args.get("mode", "replace") == "patch":
            tool = "apply_patch"
            if not isinstance(args.get("patch"), str):
                raise ValueError("patch content is required")
        else:
            tool = "Edit"
            values["file_path"] = absolute_path(args.get("path"), cwd)
    elif tool_name == "chef_delegate":
        tool = "delegate_task"
        values["subagent_type"] = args.get("role")
    return {
        "han_runtime": "hermes", "hook_event_name": "PreToolUse",
        "session_id": session_id, "task_id": task_id, "cwd": cwd,
        "tool_name": tool, "tool_input": values,
    }


def workspace_policy(payload):
    targets = questions.tool_targets(payload)
    contexts = questions.adapter_contexts(payload, targets, mission)
    external = any(external_slots.configured(adapter) for _, adapter in contexts)
    for located, adapter in contexts:
        active = mission.read_active(mission.state_dir_of(located.project, adapter))
        if active.corrupt:
            return external, "ACTIVE is corrupt; inspect the mission before continuing"
        if active.id and (active.session != payload["session_id"] or active.runtime not in ("", "hermes")):
            return external, "this workspace belongs to another active mission session"
    return external, ""


def configuration_problem(located):
    if not located:
        return ""
    problems = []
    adapter = mission.load_adapter(located.adapter_path, problems, runtime="hermes")
    guard = adapter.get("guard") or {}
    if not isinstance(guard, dict):
        return "guard configuration must be an object"
    for key in ("allow", "deny"):
        mission.compile_patterns(guard.get(key), "guard." + key, problems)
    mission.coordinator_write_globs(adapter, problems)
    return "guard configuration is invalid" if problems else ""


def failed_policy(messages):
    return any(any(marker in message for marker in (
        "오류", "건너뜀", "무시", "확인 실패", "못 찾음", "미설정", "적용하지 않음",
    )) for message in messages)


def originating_request(session):
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return None
    if get_session_env("HERMES_SESSION_ID") == session:
        return get_session_env("HERMES_SESSION_MESSAGE_ID")
    return None


class Bridge:
    def __init__(self, lifecycle=None):
        self.bindings = {}
        self.released_slots = {}
        self.lock = threading.RLock()
        self.research = research_worker.ResearchWorkers(lifecycle)
        self.native = native_worker.NativeWorkers(lifecycle)

    def is_worker(self, session_id):
        return self.research.owns(session_id) or bool(self.native and self.native.owns(session_id))

    def cwd(self, session_id, task_id):
        key = identity(session_id, task_id)
        with self.lock:
            context = self.bindings.get(key)
        return context["cwd"] if context else native_cwd(task_id)

    def request(self, tool_name, args, session_id="", task_id="", **_):
        """Pin local file and terminal operations to this task's selected slot."""
        try:
            if tool_name == research_worker.TOOL or tool_name in native_worker.TOOLS or self.is_worker(session_id):
                return None
            cwd = self.cwd(session_id, task_id)
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            updated = dict(args)
            with self.lock:
                bound = (session_id, task_id) in self.bindings
            if not bound:
                return None
            if tool_name == "terminal":
                explicit = updated.get("workdir")
                updated["workdir"] = absolute_path(explicit, cwd) if explicit else cwd
            elif tool_name in ("read_file", "write_file", "search_files") or (tool_name == "patch" and updated.get("mode", "replace") != "patch"):
                if updated.get("path"):
                    updated["path"] = absolute_path(updated["path"], cwd)
            elif tool_name == "patch":
                text = updated.get("patch")
                if not isinstance(text, str):
                    raise ValueError("patch content is required")
                updated["patch"] = PATCH_HEADER.sub(lambda m: m[1] + absolute_path(m[2], cwd), text)
            return {"args": updated}
        except Exception:
            # The native request-middleware host also catches exceptions.
            return {"args": {**(args if isinstance(args, dict) else {}), ERROR_ARGUMENT: True}}

    def before_tool(self, tool_name, args, session_id="", task_id="", **_):
        try:
            if self.native and self.native.owns(session_id):
                return None if tool_name in native_worker.TOOLS else block("the worker can only use its assigned Chef tools")
            if tool_name in native_worker.TOOLS:
                return block("only the assigned Hermes worker can use this tool")
            if self.research.owns(session_id):
                return None if tool_name == research_worker.TOOL else block("the research worker can only use its workspace read tool")
            if tool_name == research_worker.TOOL:
                return block("only the assigned Hermes research worker can use this tool")
            if not isinstance(args, dict) or args.get(ERROR_ARGUMENT):
                return block("cannot resolve tool context; execution was blocked")
            origin = cwd = self.cwd(session_id, task_id)
            if tool_name == "terminal" and args.get("workdir"):
                cwd = absolute_path(args["workdir"], cwd)
            payload = canonical_payload(tool_name, args, session_id, task_id, cwd)
            payload["han_origin_cwd"] = origin
            located = mission.locate_adapter(payload)
            external, ownership = workspace_policy(payload)
            problem = configuration_problem(located) or ownership
            if problem:
                return block(problem)
            with self.lock:
                context = self.bindings.get((session_id, task_id), {})
            if external or context.get("external"):
                role = payload["tool_input"].get("subagent_type")
                if tool_name == "chef_delegate" and isinstance(role, str) and role and ":" not in role:
                    payload["tool_input"]["subagent_type"] = "chef:" + role
                directive = self.external_tool(context, tool_name, payload, located)
                if directive or (tool_name == "terminal" and mission.MARKER.fullmatch(
                        payload["tool_input"]["command"].strip())):
                    return directive
            if located and tool_name in ("delegate_task", "execute_code"):
                return block("use chef_delegate for configured role workers; native delegation/code execution is unavailable in Chef workspaces")
            if tool_name in ("terminal", "write_file", "patch", "chef_delegate"):
                outcome = subagents.decide(payload) if tool_name == "chef_delegate" else mission.decide(payload)
                if outcome.deny:
                    return block(outcome.deny)
                if failed_policy(outcome.messages):
                    return block("guard policy could not be fully evaluated")
                if payload["tool_name"] == "apply_patch":
                    for path in questions.tool_targets(payload):
                        write = {**payload, "tool_name": "Edit", "tool_input": {"file_path": path}}
                        outcome = mission.decide(write)
                        if outcome.deny:
                            return block(outcome.deny)
                        if failed_policy(outcome.messages):
                            return block("guard policy could not be fully evaluated")
            return None
        except Exception:
            return block("guard evaluation failed; execution was blocked")

    def external_tool(self, context, tool, payload, located):
        if tool not in ("terminal", "write_file", "patch", "chef_delegate", "delegate_task", "execute_code"):
            return None
        try:
            session = payload["session_id"]
            if not context.get("external"):
                inputs = payload["tool_input"]
                cwd = payload["cwd"]
                with self.lock:
                    released = self.released_slots.get(session)
                # Pass Hermes the same absolute workdir we inspect. Its shell
                # can expand relative paths and ~ in a different environment.
                if (tool == "terminal" and not inputs.get("background")
                        and inputs.get("workdir") == cwd and os.path.isabs(cwd)
                        and external_slots.detached_read_allowed(released, located, cwd, inputs["command"])):
                    return None
                return block("select the external slot in this task before running commands or writing")
            slot = external_slots.from_context(context)
            cwd = payload["cwd"]
            if not same_path(cwd, slot.cwd) and not inside_real_root(slot.cwd, cwd):
                return block("external slot working directory escaped the selected workspace")
            if tool == "terminal":
                command = payload["tool_input"]["command"]
                marker = mission.MARKER.fullmatch(command.strip())
                if marker:
                    if not same_path(cwd, slot.cwd):
                        return block("use the selected workspace root for mission markers")
                    action, mission_id = marker.group(1).lower(), marker.group(2)
                    request = originating_request(session) if action == "open" else None
                    with external_slots.workspace_transition(slot.cwd), mission_entry.active_lock(slot.state):
                        with self.lock:
                            if self.bindings.get((session, payload["task_id"])) is not context:
                                raise UsageError("the external slot binding changed before the mission marker")
                        slot = external_slots.from_context(context)
                        if action in ("open", "resume"):
                            mode = "new" if action == "open" else "resume"
                            checked = mission_entry.guard_location(payload, mode, mission_id)
                            if checked and (
                                    not same_path(checked["located"].project, slot.cwd)
                                    or not same_path(checked["located"].adapter_path, slot.adapter)
                                    or not same_path(mission.state_dir_of(slot.cwd, checked["adapter"]), slot.state)):
                                raise UsageError("mission entry receipt does not match the selected external slot")
                        slot.transition(marker, session, request)
                        if action == "close":
                            self.restore_slot_bindings(session, context)
                    return None
                if mission.contains_marker(command, "Bash"):
                    return block("run the mission marker alone")
                slot.check_command(command)
            slot.check(session)
            if tool in ("write_file", "patch") and any(
                    not inside_real_root(slot.state, target) for target in questions.tool_targets(payload)):
                return block("external slot source writes belong to a delegated worker")
        except (OSError, RuntimeError, ValueError, UsageError, mission.GuardOff, mission_entry.EntryError) as exc:
            return block(str(exc))
        return None

    def restore_slot_bindings(self, session_id, released):
        failed = False
        with self.lock:
            self.released_slots[session_id] = released
            for key, binding in list(self.bindings.items()):
                if (key[0] == session_id and binding.get("external")
                        and same_path(binding["cwd"], released["cwd"])):
                    try:
                        bind_native_cwd(key[1], binding["return_cwd"])
                    except Exception:
                        failed = True
                    finally:
                        del self.bindings[key]
        if failed:
            raise UsageError("external slot was released, but a Hermes task working directory could not be restored")

    def slot(self, args, session_id="", task_id="", **_):
        try:
            identity(session_id, task_id)
            native_cwd(task_id)  # Reject remote/container paths before any mutation.
            from chef.slots.commands import current_session_context

            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            context = current_session_context(
                args.get("project"), args.get("name"), session_id,
                resume=bool(args.get("resume", False)), dry_run=True,
            )
            if not args.get("dry_run", False):
                preview = context
                transition = external_slots.workspace_transition(preview["cwd"]) if preview.get("external") else nullcontext()
                with transition, self.lock:
                    previous = self.bindings.get((session_id, task_id))
                    if previous and previous["cwd"] != context["cwd"]:
                        raise ValueError("this task is already bound to another slot; use a separate Hermes task")
                    return_cwd = (previous or {}).get("return_cwd") or native_cwd(task_id)
                    context = current_session_context(
                        args.get("project"), args.get("name"), session_id,
                        resume=bool(args.get("resume", False)), dry_run=False,
                    )
                    if (bool(context.get("external")) != bool(preview.get("external"))
                            or (preview.get("external") and not same_path(context["cwd"], preview["cwd"]))):
                        raise UsageError("the slot configuration changed during selection")
                    bind_native_cwd(task_id, context["cwd"])
                    self.bindings[(session_id, task_id)] = {**context, "return_cwd": return_cwd}
                    if context.get("external"):
                        self.released_slots.pop(session_id, None)
            return json.dumps(context, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def delegate(self, args, session_id="", task_id="", **_):
        try:
            cwd = self.cwd(session_id, task_id)
            directive = self.before_tool("chef_delegate", args, session_id, task_id)
            if directive:
                return json.dumps({"error": directive["message"]}, ensure_ascii=False)
            from chef.runtimes.hermes.workers import delegate

            with self.lock:
                context = self.bindings.get((session_id, task_id), {})
            request = {"session_id": session_id, "task_id": task_id, "cwd": cwd,
                       "research": self.research, "native": self.native}
            if context.get("external"):
                slot = external_slots.from_context(context)
                with slot.lock():
                    slot.check(session_id)
                    result = delegate(args, **request, external=context)
            else:
                result = delegate(args, **request)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def worker_status(self, args, session_id="", task_id="", **_):
        try:
            if args or self.is_worker(session_id):
                raise ValueError("worker status is for the parent session and takes no arguments")
            from chef.infrastructure.configuration import read_adapter
            from chef.runtimes.hermes.workers import status

            cwd = self.cwd(session_id, task_id)
            located = mission.locate_adapter({"cwd": cwd, "han_runtime": "hermes"})
            if located is None:
                raise ValueError("no Chef adapter for this Hermes task; select the slot first")
            return json.dumps(status(read_adapter(located.adapter_path, "hermes"), self.research,
                                     native=self.native, cwd=cwd), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"available": False, "error": str(exc)}, ensure_ascii=False)

    def context(self, session_id="", task_id="", **_):
        if self.is_worker(session_id):
            return None
        python_context = (
            "Chef CLI Python executable: " + json.dumps(sys.executable) + ". "
            "Invoke Chef CLI scripts with this absolute interpreter path (Python 3.11+), "
            "including slot preparation before selecting a slot. "
        )
        try:
            cwd = self.cwd(session_id, task_id or session_id)
            payload = {"cwd": cwd, "han_runtime": "hermes"}
            if not mission.locate_adapter(payload):
                return {"context": python_context}
            return {"context": (
                python_context + "Chef Hermes: load skill_view(name='chef:mission') for mission work. "
                "Use chef_slot to select a ready slot in this existing session. "
                "Check chef_worker_status before opening the mission; use chef_delegate for role workers. "
                "Preserve question/approval records and actual verification evidence. "
                "Current task working directory: " + cwd
            )}
        except Exception:
            return {"context": python_context + "Chef context unavailable. Tool guards will block unresolved operations; inspect the runtime setup."}

    def verify(self, session_id="", **kwargs):
        if self.is_worker(session_id):
            return None
        try:
            task_id = kwargs.get("task_id") or session_id
            cwd = self.cwd(session_id, task_id)
            with self.lock:
                contexts = [value for (sid, _), value in self.bindings.items() if sid == session_id]
            contexts.append({"cwd": cwd})
            seen = set()
            for context in contexts:
                if context["cwd"] in seen:
                    continue
                seen.add(context["cwd"])
                payload = {"han_runtime": "hermes", "hook_event_name": "Stop", "session_id": session_id, "cwd": context["cwd"]}
                outcome = integrity.decide(payload, "hermes")
                if outcome.get("decision") == "block":
                    return {"action": "continue", "message": outcome["reason"]}
            return None
        except Exception:
            return {"action": "continue", "message": "Chef question integrity check failed; inspect the mission before reporting completion."}


SLOT_SCHEMA = {
    "name": "chef_slot", "description": "Select a ready Chef slot in this existing Hermes session; no GUI or new CLI is launched.",
    "parameters": {"type": "object", "properties": {
        "project": {"type": "string", "description": "Absolute registered project path"},
        "name": {"type": "string", "description": "Configured slot name"},
        "resume": {"type": "boolean", "default": False},
        "dry_run": {"type": "boolean", "default": False},
    }, "required": ["project", "name"], "additionalProperties": False},
}

DELEGATE_SCHEMA = {
    "name": "chef_delegate", "description": "Run a configured Chef role worker with the adapter's backend and Chef mission authority.",
    "parameters": {"type": "object", "properties": {
        "role": {"type": "string", "description": "Configured Chef role name"},
        "prompt": {"type": "string", "description": "Self-contained worker task"},
        "timeout": {"type": "integer", "minimum": 10, "maximum": 3600, "default": 600},
    }, "required": ["role", "prompt"], "additionalProperties": False},
}

WORKER_STATUS_SCHEMA = {
    "name": "chef_worker_status", "description": "Check the configured Chef worker backend and supported roles before opening a mission.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def register(ctx):
    if sys.platform not in ("darwin", "linux"):
        raise RuntimeError("Chef Hermes supports native macOS and Linux")
    if not callable(getattr(ctx, "register_middleware", None)):
        raise RuntimeError("Chef requires Hermes native tool-request middleware")
    lifecycle = getattr(ctx, "subagent_lifecycle", None)
    bridge = Bridge(lifecycle)
    ctx.register_middleware("tool_request", bridge.request)
    ctx.register_hook("pre_tool_call", bridge.before_tool)
    ctx.register_hook("pre_llm_call", bridge.context)
    ctx.register_hook("pre_verify", bridge.verify)
    ctx.register_tool("chef_slot", "chef", SLOT_SCHEMA, bridge.slot)
    ctx.register_tool("chef_delegate", "chef", DELEGATE_SCHEMA, bridge.delegate)
    ctx.register_tool("chef_worker_status", "chef", WORKER_STATUS_SCHEMA, bridge.worker_status)
    if lifecycle is not None:
        ctx.register_tool(research_worker.TOOL, research_worker.TOOLSET, research_worker.SCHEMA, bridge.research.read)
        ctx.register_hook("subagent_start", bridge.research.started)
        ctx.register_hook("subagent_stop", bridge.research.stopped)
        for tool, toolset, schema in (
            (native_worker.READ, native_worker.READ_TOOLSET, native_worker.READ_SCHEMA),
            (native_worker.WRITE, native_worker.WRITE_TOOLSET, native_worker.WRITE_SCHEMA),
            (native_worker.SHELL, native_worker.SHELL_TOOLSET, native_worker.SHELL_SCHEMA),
        ):
            def dispatch(args, _tool=tool, **kwargs):
                return bridge.native.tool(_tool, args, **kwargs)
            ctx.register_tool(tool, toolset, schema, dispatch)
        ctx.register_hook("subagent_start", bridge.native.started)
        ctx.register_hook("subagent_stop", bridge.native.stopped)
    for name in SKILLS:
        ctx.register_skill(name, PLUGIN_ROOT / "hermes" / "skills" / name / "SKILL.md", "Chef " + name)
