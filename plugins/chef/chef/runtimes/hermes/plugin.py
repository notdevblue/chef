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

from chef.guards import integrity, mission, questions, subagents
from chef.locations import PLUGIN_ROOT

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


def ownership_problem(payload):
    targets = questions.tool_targets(payload)
    contexts = questions.adapter_contexts(payload, targets, mission)
    for located, adapter in contexts:
        active = mission.read_active(mission.state_dir_of(located.project, adapter))
        if active.corrupt:
            return "ACTIVE is corrupt; inspect the mission before continuing"
        if active.id and (active.session != payload["session_id"] or active.runtime not in ("", "hermes")):
            return "this workspace belongs to another active mission session"
    return ""


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


class Bridge:
    def __init__(self):
        self.bindings = {}
        self.lock = threading.RLock()

    def cwd(self, session_id, task_id):
        key = identity(session_id, task_id)
        with self.lock:
            context = self.bindings.get(key)
        return context["cwd"] if context else native_cwd(task_id)

    def request(self, tool_name, args, session_id="", task_id="", **_):
        """Pin local file and terminal operations to this task's selected slot."""
        try:
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
            if not isinstance(args, dict) or args.get(ERROR_ARGUMENT):
                return block("cannot resolve tool context; execution was blocked")
            origin = cwd = self.cwd(session_id, task_id)
            if tool_name == "terminal" and args.get("workdir"):
                cwd = absolute_path(args["workdir"], cwd)
            payload = canonical_payload(tool_name, args, session_id, task_id, cwd)
            payload["han_origin_cwd"] = origin
            located = mission.locate_adapter(payload)
            problem = configuration_problem(located) or ownership_problem(payload)
            if problem:
                return block(problem)
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
                with self.lock:
                    previous = self.bindings.get((session_id, task_id))
                    if previous and previous["cwd"] != context["cwd"]:
                        raise ValueError("this task is already bound to another slot; use a separate Hermes task")
                    context = current_session_context(
                        args.get("project"), args.get("name"), session_id,
                        resume=bool(args.get("resume", False)), dry_run=False,
                    )
                    bind_native_cwd(task_id, context["cwd"])
                    self.bindings[(session_id, task_id)] = context
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

            result = delegate(args, session_id=session_id, task_id=task_id, cwd=cwd)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def context(self, session_id="", task_id="", **_):
        try:
            cwd = self.cwd(session_id, task_id or session_id)
            payload = {"cwd": cwd, "han_runtime": "hermes"}
            if not mission.locate_adapter(payload):
                return None
            return {"context": (
                "Chef Hermes: load skill_view(name='chef:mission') for mission work. "
                "Use chef_slot to select a ready slot in this existing session. "
                "Use chef_delegate for role workers; native delegate_task is unavailable. "
                "Preserve question/approval records and actual verification evidence. "
                "Current task working directory: " + cwd
            )}
        except Exception:
            return {"context": "Chef context unavailable. Tool guards will block unresolved operations; inspect the runtime setup."}

    def verify(self, session_id="", **kwargs):
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
    "name": "chef_delegate", "description": "Run a configured Chef role worker through Codex CLI, preserving Chef guards and session authority.",
    "parameters": {"type": "object", "properties": {
        "role": {"type": "string", "description": "Configured Chef role name"},
        "prompt": {"type": "string", "description": "Self-contained worker task"},
    }, "required": ["role", "prompt"], "additionalProperties": False},
}


def register(ctx):
    if sys.platform not in ("darwin", "linux"):
        raise RuntimeError("Chef Hermes supports native macOS and Linux")
    if not callable(getattr(ctx, "register_middleware", None)):
        raise RuntimeError("Chef requires Hermes native tool-request middleware")
    bridge = Bridge()
    ctx.register_middleware("tool_request", bridge.request)
    ctx.register_hook("pre_tool_call", bridge.before_tool)
    ctx.register_hook("pre_llm_call", bridge.context)
    ctx.register_hook("pre_verify", bridge.verify)
    ctx.register_tool("chef_slot", "chef", SLOT_SCHEMA, bridge.slot)
    ctx.register_tool("chef_delegate", "chef", DELEGATE_SCHEMA, bridge.delegate)
    for name in SKILLS:
        ctx.register_skill(name, PLUGIN_ROOT / "hermes" / "skills" / name / "SKILL.md", "Chef " + name)
