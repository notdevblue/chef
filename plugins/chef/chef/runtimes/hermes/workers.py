"""Codex CLI workers for Hermes; authentication stays in the user's Codex home."""

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
from chef.infrastructure.fingerprints import implementation_digest
from chef.infrastructure.locking import FileLock, process_alive
from chef.infrastructure.paths import inside_root
from chef.infrastructure.process import child_env
from chef.locations import PLUGIN_ROOT

WORKER_ENV = "CHEF_HERMES_WORKER"
ROLES = ("researcher", "dev-small", "dev-medium", "verifier", "code-reviewer", "reviewer-small")
READ_ONLY = ("researcher", "code-reviewer", "reviewer-small")


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
    if cwd != data["cwd"] and not inside_root(data["cwd"], cwd):
        raise ValueError("Codex worker working directory escaped its assigned workspace")
    inputs = payload.get("tool_input")
    if isinstance(inputs, dict):
        for key in ("cwd", "workdir"):
            if inputs.get(key):
                actual = os.path.realpath(os.path.join(cwd, inputs[key]))
                if actual != data["cwd"] and not inside_root(data["cwd"], actual):
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
    from chef.guards import questions
    targets = questions.tool_targets(payload)
    if command:
        paths, trees = questions.shell_targets(command, tool, payload["cwd"], guard)
        targets.extend(paths + trees)
    for target in targets:
        path = os.path.realpath(os.path.join(payload.get("cwd") or data["cwd"], target))
        root = data["state"]
        if path == root or inside_root(root, path) or inside_root(path, root):
            return "미션 상태 파일은 Hermes 코디네이터가 관리함"
    return ""


def command_for(executable, role, cwd):
    config = tomllib.loads((PLUGIN_ROOT / "codex/agents" / f"chef-{role}.toml").read_text(encoding="utf-8"))
    instructions = config["developer_instructions"] + (
        "\nHermes 코디네이터의 역할 워커다. 미션 open/resume/close, 상태·승인 변경, "
        "하위 에이전트 생성은 하지 않는다. 요청 범위의 결과와 미확인을 반환한다."
    )
    command = [executable, "exec", "--json", "--color", "never", "--enable", "hooks", "--disable", "multi_agent",
               "--sandbox", "read-only" if role in READ_ONLY else "workspace-write",
               "--skip-git-repo-check", "-C", cwd,
               "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
               "-c", "developer_instructions=" + json.dumps(instructions, ensure_ascii=False)]
    if config.get("model"):
        command += ["--model", config["model"]]
    if config.get("model_reasoning_effort"):
        command += ["-c", "model_reasoning_effort=" + json.dumps(config["model_reasoning_effort"])]
    return command + ["-"]


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


def delegate(args, *, session_id, task_id="", cwd):
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
    if active.corrupt or not active.id or active.runtime != "hermes" or active.session != session_id:
        raise ValueError("chef_delegate requires an ACTIVE mission owned by this Hermes session")
    from chef.guards import subagents
    patterns, problem = subagents.allow_patterns(adapter)
    if problem or patterns is not None and not subagents.allows(patterns, "chef:" + role):
        raise ValueError(problem or "role is not permitted by agents.allow")
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI is required; run codex login with your subscription")
    auth = subprocess.run([executable, "login", "status"], capture_output=True, text=True, timeout=15)
    if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).lower():
        raise ValueError("Codex ChatGPT subscription login is required; API-key fallback is disabled")
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "chef/workers"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(home / ("worker-" + hashlib.sha256(os.path.realpath(cwd).encode()).hexdigest() + ".lock")), "Codex role worker"):
        with tempfile.TemporaryDirectory(prefix="call-", dir=home) as directory:
            context, ready = Path(directory) / "context.json", Path(directory) / "ready"
            guard_ready = Path(directory) / "guard-ready"
            data = {"schema": 1, "pid": os.getpid(), "session": session_id, "role": role,
                    "cwd": os.path.realpath(cwd), "state": state, "mission": active.id, "ready": str(ready),
                    "guard_ready": str(guard_ready),
                    "digest": implementation_digest()}
            context.write_text(json.dumps(data), encoding="utf-8")
            context.chmod(0o600)
            env = {**child_env(cwd), WORKER_ENV: str(context)}
            for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
                env.pop(name, None)
            protected = protected_state(state, active.id)
            # A read-only probe proves that the installed, trusted Chef hook
            # matches this deployment before any write-capable worker starts.
            probe = command_for(executable, "researcher", cwd)
            checked = run_codex(probe, "Call the shell tool once with exactly: echo han-mission open chef-worker-probe\nThe Chef worker hook must deny it. Do not attempt another command. Then return READY.", env, min(timeout, 120))
            if checked.returncode or not ready.is_file() or ready.read_text() != data["digest"] or not guard_ready.is_file() or guard_ready.read_text() != data["digest"]:
                raise ValueError("Codex Chef hooks are missing, untrusted or outdated; update and trust /hooks first")
            current = guard.read_active(state)
            if current != active or protected_state(state, active.id) != protected:
                raise ValueError("Hermes mission changed during worker preflight")
            ready.unlink()
            result = run_codex(command_for(executable, role, cwd), prompt, env, timeout)
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
