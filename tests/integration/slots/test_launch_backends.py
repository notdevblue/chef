"""Native argv, terminal readiness, and foreground execution contracts."""

import json
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.infrastructure import process
from chef.infrastructure.errors import UsageError
from chef.mission import supervisor
from chef.runtimes import launch
from chef.slots import commands


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    directory = tmp_path / "Garden with spaces"
    directory.mkdir()
    return SimpleNamespace(
        runtime="claude", terminal="foreground", project_name="Garden", autocompact=500000,
        slot_path=lambda name: str(directory), runtime_arguments_for=lambda name: ["--model", "fictional model"],
        values=lambda name: {"slot": str(directory), "python": sys.executable},
        editor="", editor_argv=[],
    )


def test_runtime_arguments_are_native_before_terminal_encoding(config):
    task = 'inspect 한글; $HOME "literal"'
    raw = launch.runtime_command(config, "north", task, resume_session="session-1")
    assert raw[0] == (launch.shutil.which("claude") or "claude")
    assert raw[1:3] == ["--model", "fictional model"]
    assert raw[-1] == "/chef:mission " + task
    assert raw[-3:-1] == ["session-1", "--"]
    assert "\\;" not in raw[-1]
    config.terminal = "wt"
    encoded = launch.mission_command(config, "north", task, resume_session="session-1")
    assert encoded[0] == "wt.exe" and "\\;" in encoded[-1]


@pytest.mark.parametrize("backend", ["foreground", "gnome-terminal", "konsole", "tmux", "macos-terminal"])
def test_posix_backend_preserves_literal_arguments_and_cwd(config, backend):
    config.terminal = backend
    task = '한글 $HOME; "data"'
    raw = launch.runtime_command(config, "north", task, resume_session="session-1")
    command = launch.mission_command(config, "north", task, resume_session="session-1")
    if backend == "foreground":
        assert command == raw
    elif backend in ("gnome-terminal", "konsole"):
        start = command.index("--") + 1 if backend == "gnome-terminal" else command.index("-e") + 1
        assert command[start:] == raw
        assert config.slot_path("north") in command
    elif backend == "tmux":
        assert shlex.split(command[-1]) == raw
        assert command[command.index("-c") + 1] == config.slot_path("north")
    else:
        assert task not in command[2]  # No user values become AppleScript source.
        assert command[-1] == "cd " + shlex.quote(config.slot_path("north")) + " && exec " + shlex.join(raw)


def test_foreground_requires_tty_and_supervisor_cannot_take_over_it(config, monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/fictional/tool")
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(isatty=lambda: False))
    assert "대화형" in launch.launcher_problem(config)
    monkeypatch.setattr(launch.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(launch.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    assert not launch.launcher_problem(config)
    assert launch.launcher_problem(config, unattended=True)


@pytest.mark.parametrize("backend", ["macos-terminal", "gnome-terminal", "konsole"])
def test_gui_backends_fail_preflight_without_gui(config, monkeypatch, backend):
    config.terminal = backend
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/fictional/tool")
    monkeypatch.setattr(launch, "gui_available", lambda _: False)
    assert "GUI" in launch.launcher_problem(config)
    with pytest.raises(UsageError, match="GUI"):
        launch.ensure_launcher(config)


def test_tmux_can_launch_unattended_without_display(config, monkeypatch):
    config.terminal = "tmux"
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/fictional/tool")
    monkeypatch.setattr(launch, "gui_available", lambda _: pytest.fail("tmux needs no GUI"))
    assert not launch.launcher_problem(config, unattended=True)


def test_supervisor_reads_project_launcher_and_keeps_hermes_current_session_only(config, monkeypatch):
    config.terminal = "tmux"
    monkeypatch.setattr(supervisor.slots, "locate", lambda *args: SimpleNamespace(adapter_path="fixture"))
    monkeypatch.setattr(supervisor.slots, "read_adapter", lambda *args: {})
    monkeypatch.setattr(supervisor.slots, "Config", lambda *args: config)
    monkeypatch.setattr(launch.shutil, "which", lambda _: "/fictional/tool")
    assert supervisor.runtime_ready("claude", {"path": config.slot_path("north")})
    config.terminal = "foreground"
    assert not supervisor.runtime_ready("claude", {"path": config.slot_path("north")})
    assert not supervisor.runtime_ready("hermes", {"path": config.slot_path("north")})


def test_actual_foreground_child_receives_native_argv_cwd_and_python(config, tmp_path):
    target = tmp_path / "child.json"
    script = tmp_path / "capture.py"
    script.write_text("import json, os, sys, shutil\nfrom pathlib import Path\n"
                      "Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:], os.getcwd(), shutil.which('python')]))\n")
    args = ["a b", "한글", 'a"b', "$HOME", "x;y", ""]
    child = launch.launch_mission(config, "north", [sys.executable, str(script), str(target), *args])
    assert launch.wait_foreground(config, child) == 0
    actual, cwd, python = json.loads(target.read_text())
    assert actual == args and os.path.samefile(cwd, config.slot_path("north"))
    selected = subprocess.run([python, "-c", "import sys; print(sys.executable)"],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    assert os.path.samefile(selected, sys.executable)


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal server environment")
@pytest.mark.parametrize("backend", ["macos-terminal", "tmux"])
def test_existing_terminal_server_receives_python_and_registry_context(config, tmp_path, monkeypatch, backend):
    config.terminal = backend
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "Registry With Spaces"))
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-secret-must-not-be-in-argv")
    command = launch.mission_command(config, "north", "inspect")
    assert not (tmp_path / "cache").exists()  # Planning/dry-run is read-only.
    seen = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda args, **kwargs: seen.append((args, kwargs)))
    launch.launch_mission(config, "north", command)
    actual, settings = seen[0]
    rendered = shlex.split(actual[-1])
    assert any(value == f"HAN_ADAPTERS_DIR={os.environ['HAN_ADAPTERS_DIR']}" for value in rendered)
    assert any(value == f"PATH={settings['env']['PATH']}" for value in rendered)
    assert "fictional-secret-must-not-be-in-argv" not in actual[-1]
    assert not any(value.startswith(("TERM=", "SHELL=")) for value in rendered)


@pytest.mark.skipif(os.name == "nt", reason="macOS executable bundle fixture")
def test_editor_array_preserves_literals_and_mac_app_resolves_real_binary(config, tmp_path, monkeypatch):
    bundle = tmp_path / "Garden Editor.app"
    executable = bundle / "Contents/MacOS/GardenEditor"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    with (bundle / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": "GardenEditor"}, stream)
    config.editor_argv = [str(bundle), "--project", "{slot}", "literal;argument"]
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    command = launch.ensure_editor(config, "north")
    assert command == [str(executable), "--project", config.slot_path("north"), "literal;argument"]


def test_posix_environment_keeps_terminal_and_shell_but_drops_parent_session(monkeypatch):
    monkeypatch.setenv("TERM", "fictional-terminal")
    monkeypatch.setenv("SHELL", "/fictional/shell")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    env = process.launch_env()
    assert "CLAUDE_CODE_SESSION_ID" not in env
    if os.name != "nt":
        assert env["TERM"] == "fictional-terminal" and env["SHELL"] == "/fictional/shell"
