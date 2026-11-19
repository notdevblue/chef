"""runtimes / launch."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from chef.infrastructure.paths import normalize
from chef.infrastructure.errors import UsageError
from chef.infrastructure.process import PARENT_SESSION_ENV, AGENT_TOOL_ENV, launch_env
from chef.infrastructure.python import python_environment
from chef.runtimes.grok import transcript as grok_transcript
from chef.slots.configuration import expand, toml_key, toml_string
from chef.slots.contracts import SESSION_NAME_LIMIT


def wt_argument(value):
    encoded = subprocess.list2cmdline([value])
    if " " in value:
        return encoded[1:-1]
    return encoded


def wt_child_command(executable, arguments):
    # Windows Terminal interprets semicolons even inside quoted arguments.
    return [wt_argument(value.replace(";", "\\;")) for value in [executable, *arguments]]


def runtime_command(config, name, task, session_task="", resume_session=""):
    if config.runtime == "hermes":
        raise UsageError("Hermes는 chef_slot으로 현재 세션에서 실행한다")
    prompts = {"claude": "/chef:mission", "codex": "$chef:chef-mission", "grok": "/chef-mission"}
    prompt = f"{prompts[config.runtime]} {task}"
    title = f"{config.project_name}:{name}"
    executable = shutil.which(config.runtime) or config.runtime
    runtime_arguments = config.runtime_arguments_for(name)
    if config.runtime == "codex":
        options = runtime_arguments + ["-c", f"model_auto_compact_token_limit={config.autocompact}"]
        if config.codex_sandbox_mode:
            options += ["--sandbox", config.codex_sandbox_mode]
        if config.codex_project_doc_max_bytes:
            options += ["-c", f"project_doc_max_bytes={config.codex_project_doc_max_bytes}"]
        for alias, path, description in config.codex_agent_entries(name):
            options += ["-c", f"agents.{toml_key(alias)}.config_file={toml_string(path)}"]
            options += ["-c", f"agents.{toml_key(alias)}.description={toml_string(description)}"]
        if resume_session:
            options += ["resume", resume_session]
        return [executable, *options, prompt]
    if config.runtime == "grok":
        # 리더 모드로 띄워야 inject 가 같은 리더에 붙어 열린 TUI 화면에 새 턴을 보여 줌
        options = runtime_arguments + [
            "--leader",
            "--leader-socket",
            grok_leader_socket(config, name),
            "--cwd",
            config.slot_path(name),
        ]
        if resume_session:
            options += ["--resume", resume_session]
        else:
            options += ["--session-id", str(uuid.uuid4())]
        return [executable, *options, "--", prompt]
    session_name = f"{name}: {session_task or task}"
    if len(session_name) > SESSION_NAME_LIMIT:
        session_name = session_name[: SESSION_NAME_LIMIT - 1] + "…"
    session = resume_session or str(uuid.uuid4())
    session_option = "--session-id"
    if resume_session:
        session_option = "--resume"
    return [executable, *runtime_arguments,
            "--autocompact",
            str(config.autocompact),
            "-n",
            session_name,
            session_option,
            session,
            "--",
            prompt,
        ]


def terminal_backend(config):
    selected = getattr(config, "terminal", "auto")
    if selected == "auto":
        return "wt" if os.name == "nt" else "foreground"
    return selected


def wt_command(config, name, task, session_task="", resume_session=""):
    """Compatibility builder for existing Windows Terminal integrations."""
    argv = runtime_command(config, name, task, session_task, resume_session)
    return ["wt.exe", "-w", "0", "nt", "--title", f"{config.project_name}:{name}",
            "-d", config.slot_path(name), *wt_child_command(argv[0], argv[1:])]


def mission_command(config, name, task, session_task="", resume_session=""):
    backend = terminal_backend(config)
    if backend == "wt":
        return wt_command(config, name, task, session_task, resume_session)
    argv = runtime_command(config, name, task, session_task, resume_session)
    cwd, title = config.slot_path(name), f"{config.project_name}:{name}"
    if backend == "foreground":
        return argv
    if backend == "gnome-terminal":
        return ["gnome-terminal", "--working-directory", cwd, "--", *argv]
    if backend == "konsole":
        return ["konsole", "--workdir", cwd, "-e", *argv]
    if backend == "tmux":
        session = "chef-" + re.sub(r"[^A-Za-z0-9_-]", "-", title) + "-" + uuid.uuid4().hex[:8]
        return ["tmux", "new-session", "-d", "-s", session, "-c", cwd, shlex.join(argv)]
    if backend == "macos-terminal":
        # User values travel as argv, never as AppleScript source. Only the
        # shell command requires POSIX quoting for Terminal's default shell.
        script = 'on run argv\ntell application "Terminal"\ndo script (item 1 of argv)\nend tell\nend run'
        shell_command = "cd " + shlex.quote(cwd) + " && exec " + shlex.join(argv)
        return ["osascript", "-e", script, shell_command]
    raise UsageError(f"지원하지 않는 터미널: {backend}")


def gui_available(backend):
    if backend == "macos-terminal":
        if sys.platform != "darwin":
            return False
        try:
            return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=3).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launcher_problem(config, unattended=False):
    if config.runtime == "hermes":
        return "Hermes는 chef_slot으로 현재 세션에서 실행한다"
    if not shutil.which(config.runtime):
        return f"런타임 CLI를 찾을 수 없음: {config.runtime}"
    backend = terminal_backend(config)
    if backend == "foreground":
        if unattended or not (sys.stdin.isatty() and (sys.stdout.isatty() or os.isatty(1))):
            return "foreground 실행에는 대화형 터미널이 필요함. slots.terminal에 GUI 터미널 또는 tmux를 명시할 것"
        return ""
    executable = {"wt": "wt.exe", "macos-terminal": "osascript", "gnome-terminal": "gnome-terminal",
                  "konsole": "konsole", "tmux": "tmux"}.get(backend)
    if not executable or not shutil.which(executable):
        return f"터미널 실행 파일을 찾을 수 없음: {executable or backend}"
    if backend in ("macos-terminal", "gnome-terminal", "konsole") and not gui_available(backend):
        return f"GUI 세션을 사용할 수 없음: {backend}. GUI 없는 실행에는 tmux를 명시할 것"
    return ""


def ensure_launcher(config):
    problem = launcher_problem(config)
    if problem:
        raise UsageError(problem)


def grok_leader_socket(config, name):
    # 슬롯마다 고정 경로. ~/.grok/leader-*.sock 이름이라 grok leader list/kill 도 찾음
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{config.project_name}-{name}").strip("-").lower()
    return os.path.join(grok_transcript.grok_home(), f"leader-{slug}.sock")


def grok_leader_record_path(config, name):
    return normalize(os.path.join(config.state_dir, "slots", f"{name}-grok-leader.json"))


def write_grok_leader_record(config, name):
    path = grok_leader_record_path(config, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "socket": grok_leader_socket(config, name),
        "slot": config.slot_path(name),
        "launched": datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)


def read_grok_leader_socket(config, name):
    try:
        with open(grok_leader_record_path(config, name), encoding="utf-8-sig") as stream:
            socket = json.load(stream).get("socket")
    except (OSError, ValueError, AttributeError):
        socket = ""
    return socket if isinstance(socket, str) and socket else grok_leader_socket(config, name)


def launch_mission(config, name, command):
    if config.runtime == "grok":
        write_grok_leader_record(config, name)
    env = python_environment(launch_env())
    backend = terminal_backend(config)
    command = list(command)
    if backend not in ("wt", "foreground"):
        # Existing terminal/tmux servers do not necessarily inherit the client
        # environment. Carry the launcher's essential paths explicitly, without
        # copying tokens/API keys into the shell command or dry-run output.
        names = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
                 "HAN_ADAPTERS_DIR", "HAN_REPO", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
                 "GROK_HOME", "HERMES_HOME", "P4CONFIG", "P4PORT", "P4USER", "P4CLIENT",
                 "SSH_AUTH_SOCK", "PYTHONUTF8", "PYTHONIOENCODING")
        prefix = ["env"]
        for key in (*PARENT_SESSION_ENV, *AGENT_TOOL_ENV, "MSYSTEM"):
            prefix += ["-u", key]
        prefix += [f"{key}={env[key]}" for key in names if key in env]
        if backend == "macos-terminal":
            command[-1] = "exec " + shlex.join([*prefix, "/bin/sh", "-c", command[-1]])
        elif backend == "tmux":
            command[-1] = shlex.join(prefix) + " " + command[-1]
        else:
            marker = "--" if backend == "gnome-terminal" else "-e"
            index = command.index(marker) + 1
            command[index:index] = prefix
    return subprocess.Popen(command, cwd=config.slot_path(name), env=env)


def wait_foreground(config, process):
    if terminal_backend(config) == "foreground" and process is not None:
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            process.wait()
            raise
    return 0


def editor_command(config, name):
    arguments = getattr(config, "editor_argv", [])
    values = config.values(name)
    if arguments:
        argv = [expand(argument, values) for argument in arguments]
    else:
        command = expand(config.editor, values)
        if os.name == "nt":
            return command
        argv = shlex.split(command)
    if not argv or not argv[0] or any("\x00" in argument for argument in argv):
        raise UsageError("slots.editor / editorArgv 에 실행 파일이 필요함")
    if argv[0].endswith(".app"):
        if sys.platform != "darwin":
            raise UsageError(".app 에디터 번들은 macOS에서만 실행할 수 있음")
        import plistlib
        try:
            with open(os.path.join(argv[0], "Contents", "Info.plist"), "rb") as stream:
                executable = plistlib.load(stream)["CFBundleExecutable"]
            if not isinstance(executable, str) or os.path.basename(executable) != executable:
                raise ValueError("invalid executable name")
            argv[0] = os.path.join(argv[0], "Contents", "MacOS", executable)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise UsageError(f"에디터 .app 실행 파일을 확인할 수 없음: {exc}") from exc
    return argv


def ensure_editor(config, name):
    command = editor_command(config, name)
    executable = shlex.split(command, posix=False)[0].strip('"') if isinstance(command, str) else command[0]
    if not shutil.which(executable):
        raise UsageError(f"에디터 실행 파일을 찾을 수 없음: {executable}")
    return command


def launch_editor(config, name):
    argv = editor_command(config, name)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        argv,
        env=python_environment(launch_env()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
