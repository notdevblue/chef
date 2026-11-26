"""Chef role admission and selected worker execution for Hermes."""

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from chef.guards import mission as guard
from chef.infrastructure.configuration import locate, read_adapter
from chef.infrastructure.errors import UsageError
from chef.infrastructure.fingerprints import implementation_digest
from chef.infrastructure.locking import FileLock, process_alive
from chef.infrastructure.paths import inside_real_root, same_path
from chef.infrastructure.process import child_env
from chef.infrastructure.python import python_environment
from chef.locations import PLUGIN_ROOT
from chef.runtimes.hermes.home import active_home, process_home

WORKER_ENV = "CHEF_HERMES_WORKER"
ROLES = ("researcher", "dev-small", "dev-medium", "verifier", "code-reviewer", "reviewer-small")
READ_ONLY = ("researcher", "code-reviewer", "reviewer-small")
# Session-flag hooks would need persisted hook trust that headless workers never get, so project
# checks use workers.guardCommand inside Chef's trusted worker hook instead of `-c hooks...`.
PINNED_SETTINGS = ("model_provider", "model_providers", "forced_login_method", "developer_instructions", "model",
                   "model_reasoning_effort", "sandbox_mode", "profile", "profiles", "features", "approval_policy", "hooks",
                   "mcp_servers")
# Dotted keys that would widen the sandbox or add unguarded write channels.
PINNED_PATHS = ("sandbox_workspace_write.writable_roots",)
# Chef's Codex PreToolUse hook has a 10 second limit; the project check must finish first.
GUARD_TIMEOUT = 8


def worker_record():
    path = os.environ.get(WORKER_ENV)
    if not path:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not process_alive(data["pid"]):
        raise ValueError("expired Hermes worker context")
    if data.get("digest") != implementation_digest():
        raise ValueError("installed Chef differs from Hermes worker source; update plugins")
    active = guard.read_active(data["state"])
    if active.corrupt or active.id != data["mission"] or active.session != data["session"] or active.runtime != "hermes":
        raise ValueError("Hermes worker mission ownership changed")
    return data


def protected_state(state, mission):
    """Detect indirect helper changes; never repair or overwrite concurrent work."""
    root = Path(state)
    folder = root / mission
    paths = [root / "ACTIVE"]
    if folder.exists():
        paths.extend(sorted(folder.rglob("*")))
    result = {}
    for path in paths:
        name = str(path.relative_to(root))
        if path.is_symlink():
            result[name] = ["link", os.readlink(path)]
        elif path.is_file():
            with path.open("rb") as stream:
                result[name] = ["file", hashlib.file_digest(stream, "sha256").hexdigest()]
    return result


def normalize_worker_payload(payload):
    data = worker_record()
    if data is None:
        return payload
    if payload.get("hook_event_name") == "SessionStart":
        Path(data["ready"]).write_text(data["digest"], encoding="utf-8")
    cwd = os.path.realpath(payload.get("cwd") or data["cwd"])
    if not same_path(cwd, data["cwd"]) and not inside_real_root(data["cwd"], cwd):
        raise ValueError("Codex worker working directory escaped its assigned workspace")
    inputs = payload.get("tool_input")
    if isinstance(inputs, dict):
        for key in ("cwd", "workdir"):
            if inputs.get(key):
                actual = os.path.realpath(os.path.join(cwd, inputs[key]))
                if not same_path(actual, data["cwd"]) and not inside_real_root(data["cwd"], actual):
                    raise ValueError("Codex worker tool working directory escaped its assigned workspace")
    return {
        **payload, "session_id": data["session"], "agent_id": "chef-codex-worker",
        "han_runtime": "hermes", "han_hermes_worker": data, "cwd": cwd,
    }


def acknowledge_guard(payload, denied):
    data = payload.get("han_hermes_worker")
    command = (payload.get("tool_input") or {}).get("command")
    if data and denied and command == "echo han-mission open chef-worker-probe":
        Path(data["guard_ready"]).write_text(data["digest"], encoding="utf-8")


def worker_denial(payload):
    data = payload.get("han_hermes_worker")
    if not data:
        return ""
    tool = str(payload.get("tool_name") or "")
    inputs = payload.get("tool_input") or {}
    if tool.endswith("spawn_agent"):
        return "Codex 역할 워커는 하위 에이전트를 만들지 않음"
    command = str(inputs.get("command") or "") if isinstance(inputs, dict) else ""
    if any(word in command for word in ("han-mission", "chef-mission", "mission_state.py", "mission_entry.py")):
        return "Codex 역할 워커는 미션 소유권·상태를 변경하지 않음"
    if data.get("external"):
        from chef.runtimes.hermes.external_slots import from_context

        try:
            slot = from_context(data["external"], gateway_home=data.get("gatewayHome"))
            slot.check_command(command)
            slot.check(data["session"], data["mission"])
        except (OSError, RuntimeError, ValueError, UsageError) as exc:
            return f"external slot reservation check failed: {exc}"
    from chef.guards import questions
    targets = questions.tool_targets(payload)
    if command:
        paths, trees = questions.shell_targets(command, tool, payload["cwd"], guard)
        targets.extend(paths + trees)
    for target in targets:
        path = os.path.realpath(os.path.join(payload.get("cwd") or data["cwd"], target))
        if data.get("external") and not inside_real_root(data["cwd"], path):
            return "external slot worker write escaped the workspace"
        root = data["state"]
        if same_path(path, root) or inside_real_root(root, path) or inside_real_root(path, root):
            return "미션 상태 파일은 Hermes 코디네이터가 관리함"
    return ""


def extra_arguments(adapter):
    """Adapter `workers.codexArguments`: project `-c key=value` overrides for every worker.

    Projects use them for sandbox details or the shell environment. Settings that
    select the account, model, instructions, sandbox mode or writable roots, hooks,
    MCP servers, features or approval policy stay pinned by Chef.
    """
    settings = adapter.get("workers", {})
    if not isinstance(settings, dict):
        raise ValueError("workers 는 객체여야 함")
    values = settings.get("codexArguments", [])
    if (not isinstance(values, list) or len(values) % 2
            or any(not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n") for value in values)):
        raise ValueError("workers.codexArguments 는 \"-c\", \"key=value\" 쌍의 문자열 배열이어야 함")
    for option, value in zip(values[::2], values[1::2]):
        key = value.split("=", 1)[0].strip()
        if option != "-c" or "=" not in value or not key:
            raise ValueError("workers.codexArguments 는 \"-c\", \"key=value\" 쌍만 허용함")
        if key.split(".", 1)[0] in PINNED_SETTINGS or any(key == path or key.startswith(path + ".") for path in PINNED_PATHS):
            raise ValueError(f"workers.codexArguments 는 Chef 가 고정한 설정을 바꿀 수 없음: {key}")
    return list(values)


def guard_command(adapter):
    """Adapter `workers.guardCommand`: project PreToolUse check run by Chef's worker hook, or None."""
    settings = adapter.get("workers", {})
    if not isinstance(settings, dict):
        raise ValueError("workers 는 객체여야 함")
    argv = settings.get("guardCommand")
    if argv is None:
        return None
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n") for value in argv)):
        raise ValueError("workers.guardCommand 는 비어 있지 않은 문자열 argv 배열이어야 함")
    return list(argv)


def backend_for(adapter):
    settings = adapter.get("workers", {})
    if not isinstance(settings, dict):
        raise ValueError("workers must be an object")
    backend = settings.get("backend", "hermes")
    if backend not in ("codex", "hermes", "claude"):
        from chef.runtimes.hermes.attachment import attachment_settings

        attachment_settings(settings, backend)
    return backend


def permitted_roles(adapter, backend):
    from chef.guards import subagents

    patterns, problem = subagents.allow_patterns(adapter)
    if problem:
        raise ValueError(problem)
    return [role for role in ROLES if patterns is None or subagents.allows(patterns, "chef:" + role)]


def codex_executable():
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI was not found on the Hermes gateway PATH")
    auth = subprocess.run([executable, "login", "status"], capture_output=True, text=True, timeout=15)
    if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).lower():
        raise ValueError("Codex ChatGPT subscription login is required; API-key fallback is disabled")
    return executable


def worker_guard(adapter):
    checker = guard_command(adapter)
    if checker and not (shutil.which(checker[0]) or os.path.isfile(checker[0])):
        raise ValueError(f"workers.guardCommand 실행 파일을 찾지 못함: {checker[0]}")
    return checker


def status(adapter, research=None, native=None, cwd=None):
    backend = backend_for(adapter)
    roles = ["chef:" + role for role in permitted_roles(adapter, backend)]
    result = {"backend": backend, "roles": roles, "available": False}
    try:
        if not roles:
            raise ValueError("agents.allow permits no roles for this worker backend")
        worker_guard(adapter)
        if backend == "hermes":
            host = native or research
            if host is None:
                raise ValueError("Hermes native worker lifecycle is unavailable in this plugin host")
            host.check_available()
            if cwd is not None:
                from chef.runtimes.hermes.contracts import preflight

                preflight(adapter.get("workers", {}), cwd, permitted_roles(adapter, backend))
            elif adapter.get("workers", {}).get("readOnlySources") or adapter.get("workers", {}).get("roleContracts"):
                raise ValueError("project role/source preflight requires the workspace directory")
            if native is None:
                result["roles"] = [role for role in roles if role == "chef:researcher"]
                if not result["roles"]:
                    raise ValueError("legacy Hermes host supports no roles permitted by agents.allow; update the host")
        elif backend == "codex":
            extra_arguments(adapter)
            codex_executable()
        elif backend == "claude":
            from chef.runtimes.hermes import claude

            claude.executable(cwd=cwd, settings_path=adapter.get("workers", {}).get("claudeSettings"))
            settings = adapter.get("workers", {}).get("claudeRoles", {})
            for role in permitted_roles(adapter, backend):
                claude.role_config(role, settings)
        else:
            from chef.runtimes.hermes.attachment import Attachment

            if native is None:
                raise ValueError("attachment requires Hermes native guarded tool execution")
            native.check_tool_execution()
            attached = Attachment(adapter["workers"], backend)
            advertised = attached.status()
            result["roles"] = [role for role in roles if role in advertised]
            if not result["roles"]:
                raise ValueError("attachment advertises no roles permitted by agents.allow")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {**result, "error": str(exc)}
    return {**result, "available": True}


def project_denial(payload, env=None):
    """Run the slot's workers.guardCommand for a worker tool call; any failure denies.

    The command receives the normalized hook payload (owner session, worker agent id)
    as JSON on stdin. Empty output allows; a deny/block decision, other output,
    a nonzero exit, a missing executable or a timeout denies. The command is the
    one fixed in the worker context when the delegation started: a worker cannot
    drop it by writing an adapter that a later lookup would find first.
    """
    data = payload.get("han_hermes_worker")
    if not data:
        return ""
    if "guard" not in data:
        raise ValueError("Hermes worker context has no fixed project guard; update Chef")
    argv = data["guard"]
    if not argv:
        return ""
    if not isinstance(argv, list) or any(not isinstance(value, str) or not value for value in argv):
        raise ValueError("invalid project guard in Hermes worker context")
    request = {key: value for key, value in payload.items() if key != "han_hermes_worker"}
    cwd = payload.get("cwd") or data["cwd"]
    try:
        result = subprocess.run(argv, input=json.dumps(request, ensure_ascii=False).encode("utf-8"), cwd=cwd,
                                capture_output=True, timeout=GUARD_TIMEOUT, env=child_env(cwd) if env is None else env)
    except (OSError, subprocess.SubprocessError) as e:
        return f"프로젝트 워커 가드를 실행하지 못함: {e}"
    if result.returncode:
        return "프로젝트 워커 가드 실패: " + result.stderr.decode("utf-8", "replace").strip()[-500:]
    text = result.stdout.decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        output = json.loads(text)
    except ValueError:
        return "프로젝트 워커 가드 출력이 JSON 이 아님"
    specific = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    if isinstance(specific, dict) and specific.get("permissionDecision") == "allow" and "decision" not in output:
        return ""
    reason = ""
    if isinstance(specific, dict):
        reason = str(specific.get("permissionDecisionReason") or "")
    if isinstance(output, dict) and not reason:
        reason = str(output.get("reason") or "")
    return "프로젝트 워커 가드: " + (reason or "거부")


def command_for(executable, role, cwd, extra=()):
    config = tomllib.loads((PLUGIN_ROOT / "codex/agents" / f"chef-{role}.toml").read_text(encoding="utf-8"))
    instructions = config["developer_instructions"] + (
        "\nHermes 코디네이터의 역할 워커다. 미션 open/resume/close, 상태·승인 변경, "
        "하위 에이전트 생성은 하지 않는다. 요청 범위의 결과와 미확인을 반환한다."
    )
    # The shared daemon has its own environment and lifecycle. A standalone
    # process must inherit this worker's hook context and process group.
    command = [executable, "--no-daemon", "exec", "--json", "--color", "never", "--enable", "hooks", "--disable", "multi_agent",
               "--sandbox", "read-only" if role in READ_ONLY else "workspace-write",
               "--skip-git-repo-check", "-C", cwd,
               "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
               "-c", "developer_instructions=" + json.dumps(instructions, ensure_ascii=False)]
    if config.get("model"):
        command += ["--model", config["model"]]
    if config.get("model_reasoning_effort"):
        command += ["-c", "model_reasoning_effort=" + json.dumps(config["model_reasoning_effort"])]
    return command + list(extra) + ["-"]


def run_codex(command, prompt, env, timeout):
    import signal

    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env,
        cwd=command[command.index("-C") + 1], start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except BaseException:
        # A worker may have started a compiler, test runner or shell. Killing
        # only Codex would leave those descendants changing the workspace.
        def stop(hard=False):
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL if hard else signal.SIGTERM)
                elif hard:
                    process.kill()
                else:
                    process.terminate()
            except OSError:
                pass  # The process or its group has already exited.

        stop()
        try:
            process.communicate(timeout=1)
        except BaseException:
            pass
        finally:
            # The leader can exit while a descendant ignores SIGTERM or has
            # closed its inherited pipes. Always finish the remaining group.
            stop(hard=True)
            try:
                process.communicate(timeout=1)
            except BaseException:
                # An escaped descendant may retain a pipe; never wait on that
                # pipe indefinitely while reaping the worker itself.
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                process.wait()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def delegate(args, *, session_id, task_id="", cwd, external=None, research=None, native=None):
    """Called only by the native Hermes tool with trusted session context."""
    if not isinstance(args, dict) or set(args) - {"role", "prompt", "timeout"}:
        raise ValueError("chef_delegate requires role, prompt and optional timeout")
    role = str(args.get("role") or "").removeprefix("chef:")
    prompt = args.get("prompt")
    timeout = args.get("timeout", 600)
    if role not in ROLES or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a supported Chef role and nonempty prompt are required")
    if type(timeout) is not int or not 10 <= timeout <= 3600:
        raise ValueError("timeout must be 10..3600 seconds")
    if not session_id or not os.path.isabs(cwd):
        raise ValueError("trusted session and absolute working directory are required")
    located = locate(cwd, "hermes")
    adapter = read_adapter(located.adapter_path, "hermes")
    state = os.path.realpath(guard.state_dir_of(located.project, adapter))
    active = guard.read_active(state)
    if active.corrupt or not guard.valid_mission_id(active.id) or active.runtime != "hermes" or active.session != session_id:
        raise ValueError("chef_delegate requires an ACTIVE mission owned by this Hermes session")
    folder = Path(state) / active.id
    if folder.is_symlink() or not (folder / "mission.md").is_file():
        raise ValueError("initialize the owned mission with mission_state.py init before delegating")
    backend = backend_for(adapter)
    if role not in permitted_roles(adapter, backend):
        raise ValueError("role is not permitted by agents.allow")
    checker = worker_guard(adapter)
    gateway_home = process_home().resolve()
    home = gateway_home / "chef/workers"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = os.stat(cwd)
    workspace_key = hashlib.sha256(f"{identity.st_dev}:{identity.st_ino}".encode()).hexdigest()
    # Every backend shares this physical-workspace gate, including native workers
    # and profiles with a different state directory. Revalidate after admission.
    with FileLock(str(home / ("worker-" + workspace_key + ".lock")), "Hermes role worker"):
        if guard.read_active(state) != active:
            raise ValueError("Hermes mission ownership changed before worker admission")
        from chef.runtimes.hermes import external_slots

        slot = external_slots.from_context(external) if external is not None else None
        if slot is not None:
            slot.check(active.session, active.id)
        protected = protected_state(state, active.id)
        try:
            if backend == "hermes":
                if native is not None:
                    settings = adapter.get("workers", {}).get("hermesRoles", {})
                    if not isinstance(settings, dict):
                        raise ValueError("workers.hermesRoles must be an object")
                    config = settings.get(role, settings.get("chef:" + role))
                    result = native.run(role, prompt, timeout, cwd, state, active, checker, slot, config=config)
                elif research is not None and role == "researcher":
                    result = research.run(prompt, timeout, cwd, state, active, checker, slot)
                else:
                    raise ValueError("Hermes native worker lifecycle is unavailable for this role")
            elif backend == "codex":
                result = _delegate_codex(adapter, role, prompt, timeout, cwd, state, active,
                                         checker, external, gateway_home, home)
            elif backend == "claude":
                from chef.runtimes.hermes import claude

                result = claude.run(role, prompt, timeout, cwd, state, active, checker,
                                    settings=adapter.get("workers", {}).get("claudeRoles", {}),
                                    settings_path=adapter.get("workers", {}).get("claudeSettings"),
                                    external=external, gateway_home=gateway_home, home=home)
            else:
                from chef.runtimes.hermes.attachment import Attachment

                if native is None:
                    raise ValueError("attachment requires Hermes native guarded tool execution")
                result = Attachment(adapter["workers"], backend).run(
                    native, role, prompt, timeout, cwd, state, active, checker, slot)
            if slot is not None:
                slot.check(active.session, active.id)
            return result
        finally:
            if guard.read_active(state) != active or protected_state(state, active.id) != protected:
                raise ValueError("protected mission state changed or mission ownership was lost during worker; inspect it before accepting results (no automatic rollback)")


def _delegate_codex(adapter, role, prompt, timeout, cwd, state, active, checker, external, gateway_home, home):
    extra = extra_arguments(adapter)
    executable = codex_executable()
    with tempfile.TemporaryDirectory(prefix="call-", dir=home) as directory:
        context, ready = Path(directory) / "context.json", Path(directory) / "ready"
        guard_ready = Path(directory) / "guard-ready"
        data = {"schema": 1, "pid": os.getpid(), "session": active.session, "role": role,
                "cwd": os.path.realpath(cwd), "state": state, "mission": active.id, "ready": str(ready),
                "guard_ready": str(guard_ready), "guard": checker,
                "digest": implementation_digest()}
        if external is not None:
            data["external"] = external
            data["gatewayHome"] = str(gateway_home)
        context.write_text(json.dumps(data), encoding="utf-8")
        context.chmod(0o600)
        env = python_environment(
            {**child_env(cwd), "HERMES_HOME": str(active_home().resolve()), WORKER_ENV: str(context)},
            cache_root=home / "interpreters",
        )
        for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
            env.pop(name, None)
        protected = protected_state(state, active.id)
        # A read-only probe proves that the installed, trusted Chef hook
        # matches this deployment before any write-capable worker starts.
        # Project settings from workers.codexArguments also apply to the probe.
        probe = command_for(executable, "researcher", cwd, extra)
        checked = run_codex(probe, "Call the shell tool once with exactly: echo han-mission open chef-worker-probe\nThe Chef worker hook must deny it. Do not attempt another command. Then return READY.", env, min(timeout, 120))
        if checked.returncode or not ready.is_file() or ready.read_text() != data["digest"] or not guard_ready.is_file() or guard_ready.read_text() != data["digest"]:
            raise ValueError("Codex Chef hooks are missing, untrusted or outdated; update and trust /hooks first")
        current = guard.read_active(state)
        if current != active or protected_state(state, active.id) != protected:
            raise ValueError("Hermes mission changed during worker preflight")
        ready.unlink()
        result = run_codex(command_for(executable, role, cwd, extra), prompt, env, timeout)
        if protected_state(state, active.id) != protected:
            raise ValueError("protected mission state changed during worker; inspect it before accepting results (no automatic rollback)")
        if not ready.is_file() or ready.read_text() != data["digest"]:
            raise ValueError("Codex worker did not acknowledge its Chef hooks")
        messages = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                messages.append(item.get("text", ""))
        return {"ok": result.returncode == 0, "runtime": "codex", "role": "chef:" + role,
                "exit_code": result.returncode, "result": "\n".join(messages),
                "error": result.stderr[-2000:] if result.returncode else ""}
