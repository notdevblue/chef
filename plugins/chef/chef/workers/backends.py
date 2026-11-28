"""Explicit subscription CLI backends shared by all coordinator runtimes."""

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from pathlib import Path

from chef.infrastructure.fingerprints import execution_digest as implementation_digest
from chef.infrastructure.process import child_env
from chef.infrastructure.python import python_environment
from chef.infrastructure.storage import atomic_write_text, make_private_directory
from chef.locations import PLUGIN_ROOT
from chef.runtimes.hermes import claude
from chef.runtimes.hermes.workers import extra_arguments
from chef.workers import context, edits, observations, process

RUNTIMES = ("claude", "grok", "codex")
ALIASES = {"research": "researcher", "implement": "dev-medium", "implementSmall": "dev-small",
           "verify": "verifier", "review": "code-reviewer", "reviewSmall": "reviewer-small", "audit": "code-reviewer"}
ROLES = ("researcher", "dev-small", "dev-medium", "verifier", "code-reviewer", "reviewer-small")


class RateLimited(RuntimeError):
    """A recognized quota failure from a reaped pre-work probe, safe to reroute."""

    def __init__(self, runtime, evidence):
        self.runtime = runtime
        self.before_work = True
        self.evidence = evidence
        super().__init__(evidence.get("error") or runtime + " quota exhausted before work")


def role_name(role):
    role = role.removeprefix("chef:")
    return ALIASES.get(role, role)


def environment(runtime, cwd):
    env = child_env(cwd)
    for key in ("CHEF_HERMES_WORKER", context.ENV, "GROK_SESSION_ID", "GROK_HOOK_EVENT",
                "GROK_WORKSPACE_ROOT", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_ROOT",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "XAI_API_KEY", "XAI_BASE_URL",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        env.pop(key, None)
    return env


def role_config(runtime, role, settings=None):
    settings = settings or {}
    role = role_name(role)
    project_roles = settings.get("projectRoles", {})
    actual = next((project_roles[key] for key, name in ALIASES.items()
                   if name == role and key in project_roles), "chef:" + role)
    definition = actual.removeprefix("chef:")
    custom = settings.get("roleDefinitions", {}).get(runtime, {}).get(definition)
    if custom:
        path = Path(custom)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError("worker role definition must be an absolute regular file")
    elif definition not in ROLES:
        raise ValueError("custom worker role has no target runtime definition")
    elif runtime == "codex":
        path = PLUGIN_ROOT / "codex/agents" / ("chef-" + definition + ".toml")
    else:
        path = PLUGIN_ROOT / ("agents" if runtime == "claude" else "grok/agents") / (definition + ".md")
    if runtime == "codex":
        value = tomllib.loads(path.read_text(encoding="utf-8"))
        config = {"model": value.get("model", ""), "effort": value.get("model_reasoning_effort", ""),
                  "instructions": value["developer_instructions"]}
    else:
        parts = path.read_text(encoding="utf-8").split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("invalid worker role frontmatter")
        header = dict(line.split(":", 1) for line in parts[1].splitlines() if ":" in line)
        config = {"model": header.get("model", "").strip(), "effort": header.get("effort", "").strip(),
                  "instructions": parts[2].strip()}
    overrides = settings.get(runtime + "Roles", {}).get(role, {})
    if not isinstance(overrides, dict) or set(overrides) - {"model", "effort"}:
        raise ValueError("worker role overrides accept model and effort only")
    config.update(overrides)
    for key in ("model", "effort", "instructions"):
        if not isinstance(config[key], str) or "\x00" in config[key]:
            raise ValueError("invalid worker role " + key)
    config["instructions"] += ("\nYou are a leaf Chef role worker. Read AGENTS.md, CLAUDE.md and the exact "
        "references in your assigned brief. Preserve other changes. Return observed evidence and unverified "
        "scope. Do not delegate, change mission or approval state, submit, push, or broaden assigned files.")
    return config


def role_policy(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()


def auth(runtime, cwd, settings=None):
    settings = settings or {}
    path = shutil.which(runtime)
    if not path:
        raise ValueError(runtime + " CLI is not on PATH")
    env = environment(runtime, cwd)
    if runtime == "claude":
        # Preserve only the subscription account in the operator's selected home.
        result = process.run([path, "auth", "status", "--json"], "", cwd=cwd, env=env, timeout=20)
        value = json.loads(result.stdout)
        if result.returncode or value.get("loggedIn") is not True or value.get("authMethod") != "claude.ai":
            raise ValueError("Claude subscription login is required")
        version = process.run([path, "--version"], "", cwd=cwd, env=env, timeout=20)
        import re

        match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", version.stdout)
        if version.returncode or not match or tuple(map(int, match.groups())) < (2, 1, 295):
            raise ValueError("Claude 2.1.295 or newer is required for blocking worker hooks")
    elif runtime == "codex":
        result = process.run([path, "login", "status"], "", cwd=cwd, env=env, timeout=20)
        if result.returncode or "chatgpt" not in (result.stdout + result.stderr).lower():
            raise ValueError("Codex ChatGPT subscription login is required")
        extra_arguments({"workers": settings})
    elif runtime == "grok":
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": 1, "clientCapabilities": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "authenticate",
                     "params": {"methodId": "cached_token", "_meta": {"headless": True}}}]
        result = process.run([path, "agent", "--no-leader", "stdio"],
                             "\n".join(json.dumps(row) for row in requests) + "\n", cwd=cwd, env=env, timeout=20,
                             complete=lambda output: any(row.get("id") == 2 for row in events(output.decode("utf-8", "replace"))))
        replies = {row.get("id"): row for row in events(result.stdout)}
        if "result" not in replies.get(1, {}) or "result" not in replies.get(2, {}):
            raise ValueError("Grok cached subscription login is required")
    else:
        raise ValueError("unsupported worker runtime")
    return path


def tools_for(role):
    role = role_name(role)
    tools = ["Read", "Grep", "Glob"]
    tools.extend(("Bash", "PowerShell"))
    if role in ("dev-small", "dev-medium"):
        tools.extend(("Write", "Edit", "apply_patch"))
    return tools


def candidates(adapter, cwd, role, *, native=None, host_tools=(), allowed_runtimes=RUNTIMES):
    settings = {**adapter.get("workers", {}), "projectRoles": adapter.get("roles", {})}
    result = []
    for runtime in RUNTIMES:
        if runtime not in allowed_runtimes:
            continue
        item = {"runtime": runtime, "available": False, "roles": [role, role_name(role), "chef:" + role_name(role)],
                "tools": tools_for(role), "capabilities": tools_for(role), "model": "", "effort": ""}
        try:
            config = role_config(runtime, role, settings)
            if runtime == native:
                item["tools"] = list(host_tools)
                item["capabilities"] = list(host_tools)
            else:
                auth(runtime, cwd, settings)
                if runtime == "claude":
                    item["tools"] = [tool for tool in item["tools"] if tool not in ("PowerShell", "apply_patch")]
                    item["capabilities"] = list(item["tools"])
                elif runtime == "grok":
                    item["tools"] = [tool for tool in item["tools"] if tool not in ("Bash", "PowerShell", "apply_patch")]
                    if role_name(role) in ("dev-small", "dev-medium"):
                        item["tools"].append("Delete")
                    item["capabilities"] = list(item["tools"])
            item.update(available=True, model=config["model"], effort=config["effort"])
            item["policy"] = role_policy(config)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            item["error"] = str(exc)
        result.append(item)
    return result


def events(stdout):
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def parse_result(runtime, result, role, *, final_message=False):
    if runtime == "claude":
        parsed = claude.parse_result(result, role)
        parsed["session_id"] = next((row.get("session_id", "") for row in events(result.stdout)
                                    if row.get("type") == "result"), "")
        if not parsed["ok"]:
            parsed.update(rate_limit_evidence(result.stdout))
        return parsed
    messages = []
    terminal = False
    failed = False
    session = ""
    for event in events(result.stdout):
        if runtime == "codex":
            if event.get("type") == "thread.started":
                session = str(event.get("thread_id") or "")
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                messages.append(str(item.get("text") or ""))
            terminal = terminal or event.get("type") == "turn.completed"
            failed = failed or event.get("type") in ("turn.failed", "error")
        else:
            session = str((event.get("params") or {}).get("sessionId") or event.get("session_id") or session)
            if event.get("type") == "tool_call":
                messages = []
            if event.get("type") == "text" and isinstance(event.get("data"), str):
                messages.append(event["data"])
            if event.get("type") == "end":
                session = str(event.get("sessionId") or session)
                terminal = event.get("stopReason") == "end_turn"
                failed = failed or not terminal
                if isinstance(event.get("structuredOutput"), dict):
                    messages = [json.dumps(event["structuredOutput"], ensure_ascii=False)]
            update = (event.get("params") or {}).get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                messages.append(str((update.get("content") or {}).get("text") or ""))
            terminal = terminal or kind == "turn_completed"
            failed = failed or bool(event.get("error")) or kind == "error"
            # Headless JSON may finish with a Claude-compatible result event.
            if event.get("type") == "result":
                terminal = event.get("is_error") is not True and event.get("subtype") in (None, "success")
                failed = failed or event.get("is_error") is True
                if event.get("result"):
                    messages = [str(event["result"])]
                if isinstance(event.get("structured_output"), dict):
                    messages = [json.dumps(event["structured_output"])]
    ok = result.returncode == 0 and terminal and not failed
    if runtime == "codex" and final_message:
        messages = messages[-1:]
    parsed = {"ok": ok, "runtime": runtime, "role": "chef:" + role, "exit_code": result.returncode,
            "session_id": session,
            "result": "".join(messages) if runtime == "grok" else "\n".join(messages),
            "error": "" if ok else "Worker did not produce a successful terminal event. " + result.stderr[-1500:]}
    if not ok:
        parsed.update(rate_limit_evidence(result.stdout))
    return parsed


def rate_limit_evidence(stdout):
    for event in events(stdout):
        if event.get("type") not in ("result", "turn.failed", "error"):
            continue
        error = event.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else ""
        message = str(event.get("result") or (error.get("message") if isinstance(error, dict) else error) or event.get("message") or "")
        known = code in (429, "rate_limit_exceeded", "rate_limited", "usage_limit_reached")
        failed = event.get("is_error") is True or event.get("type") in ("turn.failed", "error")
        text = message.lower()
        if known or failed and any(fragment in text for fragment in (
                "you've hit your 5-hour limit", "you've hit your weekly limit", "you've hit your usage limit",
                "rate limit exceeded", "usage limit reached", "quota exceeded")):
            value = {"errorCode": "rate_limited", "error": message or "service reported rate limit"}
            retry = error.get("retry_after_seconds") if isinstance(error, dict) else None
            if type(retry) in (int, float) and retry >= 0:
                value["retryAfter"] = retry
            return value
    return {}


def command(runtime, executable, role, config, cwd, directory, settings, *, probe=False):
    if runtime == "claude":
        tools = ["Bash"] if probe else [tool for tool in tools_for(role) if tool not in ("apply_patch", "PowerShell")]
        effective = directory / "settings.json"
        claude.write_settings(effective, tools, {"forceLoginMethod": "claudeai"})
        argv = claude.command_for(executable, role, config, settings_path=effective, probe=probe, tools=tools)
        argv.remove("--no-session-persistence")
        return argv + ["--resume" if config.get("resume") and not probe else "--session-id", config["session"]]
    if runtime == "codex":
        argv = [executable, "--no-daemon", "exec", "--json", "--color", "never", "--enable", "hooks",
                "--disable", "multi_agent", "--sandbox", "read-only" if probe or role in context.READ_ONLY else "workspace-write",
                "--skip-git-repo-check", "-C", cwd, "-c", 'model_provider="openai"',
                "-c", 'forced_login_method="chatgpt"', "-c", "developer_instructions=" + json.dumps(config["instructions"])]
        if config["model"] not in ("", "inherit"):
            argv.extend(("--model", config["model"]))
        if config["effort"]:
            argv.extend(("-c", "model_reasoning_effort=" + json.dumps(config["effort"])))
        argv += extra_arguments({"workers": settings})
        if config.get("resume") and not probe:
            argv += ["resume", config["session"]]
        return argv + ["-"]
    prompt = directory / "prompt.txt"
    tools = ["Bash"] if probe else ["Read", "Grep", "Glob", *( ["Bash"] if role in context.READ_ONLY else [])]
    argv = [executable, "--cwd", cwd, "--prompt-file", str(prompt), "--rules", config["instructions"],
            "--output-format", "streaming-json", "--no-subagents", "--no-memory", "--permission-mode", "dontAsk",
            "--tools", ",".join(tools),
            "--resume" if config.get("resume") and not probe else "--session-id", config["session"]]
    if os.name != "nt":
        argv.extend(("--sandbox", "read-only" if probe or role in context.READ_ONLY else "workspace"))
    for tool in tools:
        argv.extend(("--allow", tool))
    argv.extend(("--deny", "MCPTool(*)"))
    if probe:
        argv.extend(("--deny", "Bash"))
    if not probe:
        denied = "PowerShell,Write,Edit,MultiEdit,NotebookEdit,apply_patch,spawn_subagent"
        if role not in context.READ_ONLY:
            denied = "Bash," + denied
        argv.extend(("--disallowed-tools", denied))
        if role in ("dev-small", "dev-medium"):
            argv.extend(("--json-schema", json.dumps(edits.SCHEMA)))
    if config["model"] not in ("", "inherit"):
        argv.extend(("--model", config["model"]))
    if config["effort"]:
        argv.extend(("--reasoning-effort", config["effort"]))
    return argv


def run(runtime, role, prompt, *, cwd, owner, settings=None, timeout=600, context_dir,
        cancelled=None, on_start=None):
    settings = settings or {}
    role = role_name(role)
    config = role_config(runtime, role, settings)
    policy = role_policy(config)
    if owner.get("expectedPolicy") and owner["expectedPolicy"] != policy:
        raise ValueError("worker role policy changed after routing")
    resumed = owner.get("resumeSession")
    if resumed:
        uuid.UUID(resumed)
    config.update(session=resumed or str(uuid.uuid4()), resume=bool(resumed))
    executable = auth(runtime, cwd, settings)
    durable_directory = Path(context_dir)
    make_private_directory(durable_directory)
    temporary = tempfile.TemporaryDirectory(prefix="chef-grok-hook-") if runtime == "grok" else None
    directory = Path(temporary.name) if temporary else durable_directory
    context_path = directory / "context.json"
    data = {"schema": 1, "pid": os.getpid(), "backend": runtime, "role": role,
            "owner": owner, "cwd": os.path.realpath(cwd), "digest": implementation_digest(),
            "phase": "probe", "ready": str(directory / "ready"), "guardReady": str(directory / "guard-ready"),
            "guard": settings.get("guardCommand"), "contextPath": str(context_path),
            "cancelPath": str(owner.get("cancelPath") or durable_directory / "cancel.json")}
    context_path.write_text(json.dumps(data), encoding="utf-8")
    context_path.chmod(0o600)
    env = python_environment({**environment(runtime, cwd), context.ENV: str(context_path)},
                             cache_root=directory / "interpreters")
    active_path = Path(owner["state"]) / "ACTIVE"
    protected = active_path.read_bytes()
    before = edits.snapshot(cwd, owner.get("files", [])) if runtime == "grok" else {}
    if runtime == "grok" and role in ("dev-small", "dev-medium"):
        prompt += ("\nReturn one JSON object with summary and changes. Each change has path, beforeSha256, "
                   "and complete UTF-8 content. Use content:null to delete an existing assigned file. "
                   "Renames require a deletion and creation with both exact assigned paths. "
                   "You have read tools only; the parent applies validated edits. "
                   "Use only these exact assigned files and preimage hashes: " + json.dumps(before))

    def execute(text, probe):
        if role_policy(role_config(runtime, role, settings)) != policy:
            raise ValueError("worker role policy changed before execution")
        (directory / "prompt.txt").write_text(text, encoding="utf-8")
        execution_config = {**config, "session": str(uuid.uuid4()), "resume": False} if probe else config
        argv = command(runtime, executable, role, execution_config, cwd, directory, settings, probe=probe)
        def should_stop():
            blocker = directory / "blocked.json"
            repeated = blocker.exists() and json.loads(blocker.read_text(encoding="utf-8")).get("count", 0) >= 2
            return repeated or bool(cancelled and cancelled())

        result = process.run(argv, text if runtime != "grok" else "", cwd=cwd, env=env,
                             timeout=min(timeout, 120) if probe else timeout, cancelled=should_stop, on_start=on_start)
        label = "probe" if probe else "work"
        atomic_write_text(durable_directory / (label + ".stdout.jsonl"), result.stdout)
        atomic_write_text(durable_directory / (label + ".stderr.log"), result.stderr)
        return result

    try:
        checked = execute("Call the shell tool once with exactly: " + context.PROBE +
                          "\nThe Chef hook must deny it. Do not retry. Return READY.", True)
        probe_result = parse_result(runtime, checked, role)
        if probe_result.get("errorCode") == "rate_limited":
            raise RateLimited(runtime, {**probe_result, "runtime": runtime})
        if not probe_result["ok"]:
            raise ValueError("worker guard probe did not finish successfully")
        for key in ("ready", "guardReady"):
            marker = Path(data[key])
            if not marker.is_file() or marker.read_text(encoding="utf-8") != data["digest"]:
                raise ValueError("Chef worker hooks missing, untrusted or outdated")
        if active_path.read_bytes() != protected:
            raise ValueError("protected state changed during worker probe")
        Path(data["ready"]).unlink()
        data["phase"] = "work"
        context_path.write_text(json.dumps(data), encoding="utf-8")
        try:
            completed = execute(prompt, False)
        except process.Cancelled:
            blocker = directory / "blocked.json"
            if not blocker.exists():
                raise
            blocked = json.loads(blocker.read_text(encoding="utf-8"))
            return {"ok": False, "runtime": runtime, "errorCode": "guard_blocked", "error": blocked["reason"],
                    "blocked": blocked, "result": "Stopped after repeated guard denials; inspect the recorded command and scope."}
        result = parse_result(runtime, completed, role, final_message=bool(owner.get("structuredReview")))
        if cancelled and cancelled():
            raise process.Cancelled("worker cancelled before accepting result")
        if role_policy(role_config(runtime, role, settings)) != policy:
            raise ValueError("worker role policy changed before accepting result")
        ready = Path(data["ready"])
        if (not ready.is_file() or ready.read_text(encoding="utf-8") != data["digest"]
                or active_path.read_bytes() != protected or implementation_digest() != data["digest"]):
            raise ValueError("worker lost its hook acknowledgement or changed protected state")
        if result["ok"] and runtime == "grok" and role in ("dev-small", "dev-medium"):
            result["result"] = edits.apply(result["result"], cwd=cwd, before=before, data=data, directory=durable_directory)
        result.update(model=config["model"], effort=config["effort"], isolation="cooperative-hooks")
        if runtime == "grok":
            result["isolation"] = "read-tools-with-validated-edit-application"
        if runtime in ("claude", "grok"):
            result["session_id"] = result.get("session_id") or config["session"]
        result.update(observations.collect(runtime, completed.stdout, result.get("session_id", ""),
                                           cwd=cwd, environment=env, directory=durable_directory))
        return result
    finally:
        data["revoked"] = True
        context_path.write_text(json.dumps(data), encoding="utf-8")
        if temporary:
            temporary.cleanup()
