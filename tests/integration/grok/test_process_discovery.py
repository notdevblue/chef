"""Grok process ownership checks without real runtime or process mutation."""

import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chef.runtimes.grok import injection


def test_headless_injection_carries_hook_python_without_system_alias(tmp_path, monkeypatch):
    transcript = tmp_path / "updates.jsonl"
    transcript.write_text("")
    monkeypatch.setenv("GROK_BIN", "/fictional/grok")
    monkeypatch.setattr(injection, "session_files", lambda *_: str(transcript))
    monkeypatch.setattr(injection, "session_cwd", lambda *_: str(tmp_path))
    monkeypatch.setattr(injection, "wait_idle", lambda *_: [])
    monkeypatch.setattr(injection, "grok_processes", lambda: {})
    monkeypatch.setattr(injection, "open_tuis", lambda *_: [])
    monkeypatch.setattr(injection.grok_transcript, "busy_reasons", lambda *_: [])
    monkeypatch.setattr(injection, "user_turn_since", lambda *_: True)
    def spawn(argv, log, environment):
        python = shutil.which("python", path=environment["PATH"])
        actual = subprocess.check_output([python, "-c", "import sys; print(sys.prefix)"], env=environment, text=True, timeout=10)
        assert actual.strip() == sys.prefix
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(injection, "spawn_detached", spawn)
    environment = {**os.environ, "PATH": os.defpath, "XDG_CACHE_HOME": str(tmp_path / "cache")}
    assert injection.inject(str(tmp_path), "11111111-1111-4111-8111-111111111111", "fictional reply",
                            env=environment, out=lambda *_: None) == injection.OK


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(injection, "os", SimpleNamespace(name="posix", path=os.path, environ=os.environ))


def test_process_discovery_handles_spaces_native_and_node_entrypoints(posix, monkeypatch):
    names = "11 /fictional/Tools With Spaces/grok\n12 /usr/bin/node\n13 node\n14 node\n15 unrelated\n"
    arguments = (
        "11 /fictional/Tools With Spaces/grok --resume session\n"
        "12 /usr/bin/node /fictional/grok.js --resume session\n"
        "13 node /fictional/other.js grok.js\n"
        "14 node -e 'console.log(\"grok\")'\n"
        "15 /fictional/other --argument grok\n"
    )
    monkeypatch.setattr(injection.subprocess, "run", lambda command, **kwargs:
                        SimpleNamespace(returncode=0, stdout=names if command[-1] == "pid=,comm=" else arguments))
    found = injection.grok_processes()
    assert set(found) == {11, 12}
    assert "Tools With Spaces" in found[11]


def test_node_process_matches_configured_entry_script_with_unquoted_spaces(posix, tmp_path, monkeypatch):
    script = tmp_path / "Tools With Spaces" / "index.js"
    script.parent.mkdir()
    script.write_text("// fictional entrypoint")
    monkeypatch.setenv("GROK_BIN", str(script))
    # Exercise the POSIX process-list format even on a Windows test host.
    rendered = script.as_posix()
    assert injection.node_grok_command(f"/usr/bin/node {rendered} --resume session")
    assert injection.node_grok_command(f'/usr/bin/node --require helper "{rendered}" --resume session')
    assert not injection.node_grok_command(f"/usr/bin/node /fictional/other.js {rendered}")


@pytest.mark.parametrize("failed", ["pid=,comm=", "pid=,args="])
def test_failed_process_listing_is_not_an_empty_success(posix, monkeypatch, failed):
    monkeypatch.setattr(injection.subprocess, "run", lambda command, **kwargs:
                        SimpleNamespace(returncode=1 if command[-1] == failed else 0, stdout=""))
    with pytest.raises(injection.InjectError, match="실패"):
        injection.grok_processes()


def test_process_listing_exception_fails_closed(posix, monkeypatch):
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 30)
    monkeypatch.setattr(injection.subprocess, "run", failed)
    with pytest.raises(injection.InjectError, match="실패"):
        injection.grok_processes()


def test_powershell_listing_nonzero_exit_fails_closed(monkeypatch):
    monkeypatch.setattr(injection, "os", SimpleNamespace(name="nt", path=os.path, environ=os.environ))
    monkeypatch.setattr(injection.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(injection.InjectError, match="실패"):
        injection.grok_processes()


def test_session_directory_aliases_have_same_physical_identity(tmp_path):
    target = tmp_path / "garden"
    target.mkdir()
    alias = tmp_path / "shortcut"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory links unavailable")
    assert injection.same_dir(target, alias)
    assert not injection.same_dir(target, tmp_path / "other")
