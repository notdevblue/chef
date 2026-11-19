"""runtimes / launch."""

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime

from chef.infrastructure.paths import normalize
from chef.infrastructure.process import launch_env
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


def wt_command(config, name, task, session_task="", resume_session=""):
    prompts = {"claude": "/chef:mission", "codex": "$chef:chef-mission", "grok": "/chef-mission"}
    prompt = f"{prompts[config.runtime]} {task}"
    title = f"{config.project_name}:{name}"
    executable = shutil.which(config.runtime) or config.runtime
    command = ["wt.exe", "-w", "0", "nt", "--title", title, "-d", config.slot_path(name)]
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
        return command + wt_child_command(executable, options + [prompt])
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
        return command + wt_child_command(executable, options + ["--", prompt])
    session_name = f"{name}: {session_task or task}"
    if len(session_name) > SESSION_NAME_LIMIT:
        session_name = session_name[: SESSION_NAME_LIMIT - 1] + "…"
    session = resume_session or str(uuid.uuid4())
    session_option = "--session-id"
    if resume_session:
        session_option = "--resume"
    return command + wt_child_command(
        executable,
        runtime_arguments
        + [
            "--autocompact",
            str(config.autocompact),
            "-n",
            session_name,
            session_option,
            session,
            "--",
            prompt,
        ],
    )


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
    if config.runtime == "grok" and "--leader" in command:
        write_grok_leader_record(config, name)
    subprocess.Popen(command, env=launch_env())


def launch_editor(config, name):
    command = expand(config.editor, config.values(name))
    argv = command if os.name == "nt" else shlex.split(command)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        argv,
        env=launch_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
