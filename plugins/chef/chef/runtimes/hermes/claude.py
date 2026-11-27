"""Optional Claude Code workers with per-call, fail-closed hooks.

This is a guarded CLI process, not an operating-system sandbox. Admission and
the physical-workspace lease belong to the Hermes dispatcher.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from chef.guards import mission, questions
from chef.infrastructure.fingerprints import execution_digest as implementation_digest
from chef.infrastructure.paths import inside_real_root, same_path
from chef.infrastructure.process import child_env
from chef.infrastructure.python import python_environment
from chef.locations import PLUGIN_ROOT
from chef.runtimes.hermes.home import active_home, process_home

READ_ONLY = ("researcher", "code-reviewer", "reviewer-small")
ROLES = (*READ_ONLY, "dev-small", "dev-medium", "verifier")
READ_TOOLS = ("Read", "Grep", "Glob", "WebFetch", "WebSearch")
WRITE_TOOLS = ("Read", "Grep", "Glob", "Bash", "Write", "Edit")
VERIFY_TOOLS = ("Read", "Grep", "Glob", "Bash")
PROBE = "echo han-mission open chef-worker-probe"
WORKER_ENV = "CHEF_HERMES_WORKER"
AUTH_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh",
             "forceLoginMethod", "forceLoginOrgUUID", "modelOverrides", "env")


def environment(cwd):
    env = child_env(cwd)
    # Keep the operator's selected authentication/provider/profile. Project
    # settings are isolated by CLI flags, independently from account selection.
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"):
        env.pop(key, None)
    return env


def auth_settings(settings_path):
    if settings_path is None:
        return {}
    if not isinstance(settings_path, str) or not os.path.isabs(settings_path):
        raise ValueError("workers.claudeSettings must be an absolute JSON file path")
    try:
        value = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read workers.claudeSettings JSON file") from exc
    if not isinstance(value, dict) or set(value) - set(AUTH_KEYS):
        raise ValueError("workers.claudeSettings accepts only documented authentication/provider settings")
    for key, setting in value.items():
        if key in ("env", "modelOverrides"):
            if not isinstance(setting, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                    or not k or "\x00" in k or "\x00" in v for k, v in setting.items()):
                raise ValueError(f"Claude authentication {key} must map nonempty string keys to strings")
            if key == "env" and any(k.startswith(("CHEF_", "HERMES_", "HAN_", "PYTHON"))
                                     or "=" in k for k in setting):
                raise ValueError("Claude authentication env cannot replace Chef worker execution context")
        elif key == "forceLoginOrgUUID" and isinstance(setting, list):
            if any(not isinstance(item, str) or not item.strip() or "\x00" in item for item in setting):
                raise ValueError("Claude authentication forceLoginOrgUUID must contain nonempty strings")
        elif not isinstance(setting, str) or not setting.strip() or "\x00" in setting:
            raise ValueError(f"Claude authentication {key} must be a nonempty string")
    if "forceLoginMethod" in value and value["forceLoginMethod"] not in ("claudeai", "console", "gateway"):
        raise ValueError("Claude authentication forceLoginMethod is invalid")
    return value


def write_settings(path, tools, authentication):
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({**authentication, **fixed_settings(tools)}, stream)


def selected_settings(cwd, env):
    """Known user/start/local-root sources; managed settings remain CLI-owned."""
    root = Path(cwd)
    profile = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    if not profile.is_absolute():
        profile = root / profile
    paths = [profile / "settings.json", root / ".claude/settings.json", root / ".claude/settings.local.json"]
    repository = next((candidate for candidate in (root, *root.parents)
                       if (candidate / ".git").exists()), None)
    if os.name != "nt" and repository is not None and not same_path(repository, Path.home()):
        owned = all(not path.exists() or path.stat().st_uid == os.getuid()
                    for path in (repository, repository / ".git", repository / ".claude"))
        if owned:
            git = shutil.which("git")
            if not git:
                raise ValueError("cannot determine Claude local settings source without Git")
            result = subprocess.run([git, "-C", str(repository), "worktree", "list", "--porcelain", "-z"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15, env=env, cwd=cwd)
            first = result.stdout.split("\x00", 1)[0]
            if result.returncode or not first.startswith("worktree "):
                raise ValueError("cannot determine Claude main-worktree local settings source")
            paths.append(Path(first[len("worktree "):]) / ".claude/settings.local.json")
    return list(dict.fromkeys(path.absolute() for path in paths))


def require_explicit_routing(cwd, env):
    # A CLI auth status describes the account/provider category, not every
    # gateway endpoint. Do not infer equivalence from that category alone.
    for path in selected_settings(cwd, env):
        if not path.exists() and not path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read selected Claude settings file: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"selected Claude settings must be a JSON object: {path}")
        if any(key in value and value[key] not in (None, {}, "") for key in AUTH_KEYS):
            raise ValueError(f"selected Claude settings contain authentication/provider routing: {path}; "
                             "provide workers.claudeSettings explicitly")


def executable(cwd=None, settings_path=None):
    cwd = os.path.abspath(cwd or os.getcwd())
    authentication = auth_settings(settings_path)
    with tempfile.TemporaryDirectory(prefix="chef-claude-auth-") as directory:
        effective = Path(directory) / "settings.json"
        write_settings(effective, READ_TOOLS, authentication)
        return checked_executable(cwd, effective, environment(cwd), explicit=settings_path is not None)


def checked_executable(cwd, effective, env, *, explicit):
    path = shutil.which("claude")
    if not path:
        raise ValueError("Claude Code CLI was not found on the Hermes gateway PATH")
    version = subprocess.run([path, "--version"], capture_output=True, text=True,
                             encoding="utf-8", timeout=15, env=env, cwd=cwd)
    matched = re.match(r"\s*(\d+)\.(\d+)\.(\d+)(?:\s|$)", version.stdout)
    if version.returncode or not matched or tuple(map(int, matched.groups())) < (2, 1, 295):
        raise ValueError("Claude Code 2.1.295 or newer is required for fail-closed worker hooks")
    if not explicit:
        require_explicit_routing(cwd, env)
    def status(prefix):
        result = subprocess.run([path, *prefix, "auth", "status", "--json"], capture_output=True,
                                text=True, encoding="utf-8", timeout=15, env=env, cwd=cwd)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            value = {}
        return value if result.returncode == 0 and isinstance(value, dict) else {}

    selected = status([])
    actual = status(["--settings", str(effective), "--setting-sources", ""])
    if actual.get("loggedIn") is not True:
        raise ValueError("Claude Code CLI authentication is required; configure the selected CLI profile first")
    identity_keys = ("loggedIn", "authMethod", "apiProvider", "email", "orgId", "orgName", "organizationId", "accountId")
    if not explicit and any(selected.get(key) != actual.get(key) for key in identity_keys):
        raise ValueError("Claude authentication differs with isolated worker settings; provide workers.claudeSettings explicitly")
    return path


def role_config(role, settings=None):
    if role not in ROLES:
        raise ValueError("unsupported Chef Claude role")
    settings = {} if settings is None else settings
    if not isinstance(settings, dict):
        raise ValueError("workers.claudeRoles must be an object")
    source = (PLUGIN_ROOT / "agents" / (role + ".md")).read_text(encoding="utf-8")
    parts = source.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError("invalid bundled Claude role definition")
    # Bundled frontmatter uses scalar model/effort fields; no project YAML is
    # interpreted, and Codex role/model configuration is never consulted.
    header = dict(line.split(":", 1) for line in parts[1].splitlines() if ":" in line)
    config = {key: header.get(key, "").strip() for key in ("model", "effort")}
    override = settings.get(role, settings.get("chef:" + role, {}))
    if not isinstance(override, dict) or set(override) - {"model", "effort"}:
        raise ValueError("Claude role overrides accept only model and effort")
    config.update(override)
    if (not isinstance(config["model"], str) or not config["model"].strip()
            or any(char in config["model"] for char in "\x00\r\n")):
        raise ValueError("Claude worker model must be a nonempty string")
    if config["effort"] not in ("low", "medium", "high", "max"):
        raise ValueError("Claude worker effort must be low, medium, high or max")
    config["instructions"] = parts[2].strip() + (
        "\nYou are a leaf Chef role worker for a Hermes coordinator. Read the project's CLAUDE.md, "
        "AGENTS.md and the exact references in the brief. Do not create agents, alter mission "
        "ownership or approval/state files, or exceed the assigned files and checks. Return "
        "observed results and unverified scope to the coordinator."
    )
    return config


def tools_for(role):
    return READ_TOOLS if role in READ_ONLY else VERIFY_TOOLS if role == "verifier" else WRITE_TOOLS


def validate_payload(payload):
    data = payload["han_hermes_worker"]
    if data.get("backend") != "claude":
        raise ValueError("Claude hook requires a Claude worker context")
    if payload.get("hook_event_name") == "PreToolUse":
        tool = payload.get("tool_name")
        inputs = payload.get("tool_input")
        if data.get("phase") == "probe":
            if tool != "Bash" or not isinstance(inputs, dict) or inputs.get("command") != PROBE:
                raise ValueError("Claude worker preflight permits only the denied guard probe")
        elif data.get("phase") != "work" or tool not in tools_for(data["role"]):
            raise ValueError("tool is not permitted for this Claude role worker")
        if isinstance(inputs, dict) and inputs.get("run_in_background"):
            raise ValueError("Claude role workers may not start background commands")
        targets = questions.tool_targets(payload)
        if isinstance(inputs, dict) and tool == "Bash":
            paths, trees = questions.shell_targets(str(inputs.get("command") or ""), tool, payload["cwd"], mission)
            targets.extend(paths + trees)
        for path in targets:
            target = os.path.realpath(os.path.join(payload["cwd"], path))
            if not same_path(target, data["cwd"]) and not inside_real_root(data["cwd"], target):
                raise ValueError("Claude worker write escaped its assigned workspace")
            relative = Path(os.path.abspath(os.path.join(payload["cwd"], path))).relative_to(data["cwd"])
            if any(part.casefold() in (".hermes", ".claude", ".codex", ".grok", ".chef", ".han", ".git")
                   for part in relative.parts):
                raise ValueError("Claude worker cannot change executable policy or repository metadata")
            current = Path(data["cwd"])
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise ValueError("Claude worker writes cannot follow symlinks")
    return {**payload, "agent_id": "chef-claude-worker"}


def fixed_settings(tools):
    def hook(script):
        return {"type": "command", "command": sys.executable,
                "args": ["-P", str(PLUGIN_ROOT / "hooks" / script)], "timeout": 10,
                "onFailure": "block"}

    return {"disableAllHooks": False,
            "permissions": {"defaultMode": "dontAsk", "allow": list(tools), "deny": ["Agent", "Task"]},
            "hooks": {"SessionStart": [{"hooks": [hook("session_rules.py")]}],
                      "PreToolUse": [{"hooks": [hook("mission_guard.py")]}]}}


def command_for(path, role, config, *, settings_path, probe=False, tools=None):
    tools = ("Bash",) if probe else (tools_for(role) if tools is None else tools)
    command = [path, "-p", "--output-format", "stream-json", "--verbose",
            "--settings", str(settings_path), "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--tools", ",".join(tools), "--allowedTools", ",".join(tools),
            "--disallowedTools", "Agent,Task", "--no-session-persistence",
            "--effort", config["effort"],
            "--append-system-prompt", config["instructions"]]
    if config["model"] not in ("", "inherit"):
        command.extend(("--model", config["model"]))
    return command


def run_process(command, prompt, env, timeout, cwd):
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               start_new_session=os.name != "nt")

    def stop(hard=False):
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL if hard else signal.SIGTERM)
            else:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except BaseException:
        stop()
        try:
            process.communicate(timeout=1)
        except BaseException:
            pass
        finally:
            stop(hard=True)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            process.wait(timeout=5)
        raise
    finally:
        # Also reap background descendants after an otherwise successful result.
        stop(hard=True)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def parse_result(result, role):
    terminal = None
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            terminal = event
    ok = bool(result.returncode == 0 and terminal and terminal.get("subtype") == "success"
              and terminal.get("is_error") is not True)
    error = ""
    if not ok:
        error = "Claude worker did not return a successful terminal result"
        if terminal:
            error += ": " + str(terminal.get("errors") or terminal.get("result") or terminal.get("subtype"))[-1500:]
        elif result.stderr:
            error += ": " + result.stderr[-1500:]
    return {"ok": ok, "runtime": "claude", "role": "chef:" + role,
            "exit_code": result.returncode, "result": str((terminal or {}).get("result") or ""), "error": error}


def run(role, prompt, timeout, cwd, state, active, checker, settings=None,
        external=None, gateway_home=None, home=None, settings_path=None):
    from chef.runtimes.hermes.workers import protected_state

    config = role_config(role, settings)
    authentication = auth_settings(settings_path)
    gateway_home = Path(gateway_home) if gateway_home is not None else process_home().resolve()
    home = Path(home) if home is not None else gateway_home / "chef/workers"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    protected = protected_state(state, active.id)
    with tempfile.TemporaryDirectory(prefix="claude-call-", dir=home) as directory:
        context = Path(directory) / "context.json"
        ready, guard_ready = Path(directory) / "ready", Path(directory) / "guard-ready"
        data = {"schema": 1, "pid": os.getpid(), "session": active.session, "role": role,
                "cwd": os.path.realpath(cwd), "state": str(state), "mission": active.id,
                "ready": str(ready), "guard_ready": str(guard_ready), "guard": checker,
                "gatewayHome": str(gateway_home), "backend": "claude", "phase": "probe",
                "digest": implementation_digest()}
        if external is not None:
            data["external"] = external
        context.write_text(json.dumps(data), encoding="utf-8")
        context.chmod(0o600)
        env = python_environment({**environment(cwd), "HERMES_HOME": str(active_home().resolve()),
                                  WORKER_ENV: str(context)}, cache_root=home / "interpreters")
        effective = Path(directory) / "settings.json"
        write_settings(effective, ("Bash",), authentication)
        path = checked_executable(cwd, effective, env, explicit=settings_path is not None)
        checked = run_process(command_for(path, role, config, settings_path=effective, probe=True),
                              "Call Bash exactly once with: " + PROBE +
                              "\nThe Chef hook must deny it. Do not call another tool. Return READY.",
                              env, min(timeout, 120), cwd)
        def acknowledged(marker):
            return marker.is_file() and marker.read_text(encoding="utf-8") == data["digest"]

        if not parse_result(checked, role)["ok"] or not acknowledged(ready) or not acknowledged(guard_ready):
            raise ValueError("Claude Chef hooks are missing, blocked or outdated; worker preflight failed")
        if mission.read_active(state) != active or protected_state(state, active.id) != protected:
            raise ValueError("Hermes mission changed during Claude worker preflight")
        ready.unlink()
        data["phase"] = "work"
        context.write_text(json.dumps(data), encoding="utf-8")
        write_settings(effective, tools_for(role), authentication)
        result = run_process(command_for(path, role, config, settings_path=effective), prompt, env, timeout, cwd)
        if mission.read_active(state) != active or protected_state(state, active.id) != protected:
            raise ValueError("protected mission state changed during Claude worker; inspect before accepting results")
        if not acknowledged(ready):
            raise ValueError("Claude worker did not acknowledge its Chef hooks")
        return parse_result(result, role)
