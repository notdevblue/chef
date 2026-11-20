import os
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from chef.infrastructure import process
from chef.infrastructure import vcs as perforce
from chef.infrastructure.errors import UsageError
from chef.infrastructure.vcs import build_vcs
from chef.slots import preparation
from chef.slots.execution import windows_executable


@pytest.fixture
def tools(tmp_path, monkeypatch):
    filename = "p4.exe" if os.name == "nt" else "p4"
    paths = {
        name: tmp_path / name / filename
        for name in ("standard", "other-standard", "missing", "parent", "configured", "override")
    }
    for name, path in paths.items():
        if name != "missing":
            path.parent.mkdir()
            path.touch(mode=0o755)
    monkeypatch.delenv("P4_BIN", raising=False)
    monkeypatch.delenv("CHEF_P4", raising=False)
    monkeypatch.setenv("PATH", str(paths["parent"].parent))
    monkeypatch.setattr(process, "P4_INSTALLATIONS", tuple(
        str(paths[name]) for name in ("standard", "other-standard", "missing")
    ))
    return paths


def test_default_lookup_uses_augmented_path(tools, monkeypatch):
    which = Mock(return_value=str(tools["standard"]))
    monkeypatch.setattr(process.shutil, "which", which)
    assert process.resolve_p4() == str(tools["standard"])
    which.assert_called_once_with("p4", path=os.pathsep.join(
        str(tools[name].parent) for name in ("standard", "other-standard", "parent")
    ))


def test_installation_fallback_when_which_finds_nothing(tools, monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda *args, **kwargs: None)
    assert process.resolve_p4() == str(tools["standard"])


def test_explicit_path_and_environment_precedence(tools, monkeypatch):
    monkeypatch.setenv("P4_BIN", str(tools["override"]))
    monkeypatch.setenv("CHEF_P4", str(tools["other-standard"]))
    assert process.resolve_p4(str(tools["configured"])) == str(tools["configured"])
    assert process.resolve_p4() == str(tools["override"])
    monkeypatch.delenv("P4_BIN")
    assert process.resolve_p4() == str(tools["other-standard"])


def test_build_vcs_resolves_configured_command_and_preserves_arguments(tools, monkeypatch, tmp_path):
    which = Mock(return_value=str(tools["configured"]))
    monkeypatch.setattr(process.shutil, "which", which)
    monkeypatch.setenv("P4_BIN", str(tools["override"]))
    vcs = build_vcs({"vcs": "perforce", "stream": "//Depot/Main", "mainClient": "example",
                     "p4": ["p4-wrapper", "-p", "example.invalid:1666"]}, str(tmp_path))
    assert vcs.argv == [str(tools["configured"]), "-p", "example.invalid:1666"]
    which.assert_called_once_with("p4-wrapper", path=process.tool_search_path())


def test_perforce_uses_resolved_executable_from_main_and_slot(tools, monkeypatch, tmp_path):
    p4 = str(tools["override"])
    monkeypatch.setenv("P4_BIN", p4)
    vcs = build_vcs({"vcs": "perforce", "stream": "//Depot/Main", "mainClient": "main"}, str(tmp_path))
    run = Mock(side_effect=[CompletedProcess([], 0, b"Client slot\n", b""),
                            CompletedProcess([], 0, b"Client name: slot\n", b"")])
    monkeypatch.setattr(perforce.subprocess, "run", run)
    assert vcs.client_exists("slot")
    slot = str(tmp_path / "slot")
    assert vcs.workspace_name_in(slot) == "slot"
    main_call, slot_call = run.call_args_list
    assert main_call.args[0] == [p4, "-c", "main", "clients", "-e", "slot"]
    assert slot_call.args[0] == [p4, "info"]
    for call, cwd in ((main_call, str(tmp_path)), (slot_call, slot)):
        assert call.kwargs["cwd"] == cwd
        assert call.kwargs["env"]["PWD"] == cwd
        assert call.kwargs["env"]["PATH"].split(os.pathsep)[0] == str(tools["override"].parent)


def test_invalid_explicit_path_is_reported(tools, monkeypatch):
    monkeypatch.setenv("P4_BIN", "relative/p4")
    with pytest.raises(UsageError, match="P4_BIN=.*absolute path"):
        process.resolve_p4()
    with pytest.raises(UsageError, match="not found at slots.p4"):
        process.resolve_p4(str(tools["missing"]))


def test_missing_p4_error_names_paths_and_overrides(tools, monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda *args, **kwargs: None)
    for path in tools.values():
        path.unlink(missing_ok=True)
    with pytest.raises(UsageError) as error:
        process.resolve_p4()
    message = str(error.value)
    for value in (*process.P4_INSTALLATIONS, str(tools["parent"].parent), "slots.p4", "P4_BIN", "CHEF_P4"):
        assert value in message


def test_child_env_and_preflight_share_paths(tools, monkeypatch, tmp_path):
    parent_path = os.pathsep.join(str(tools[name].parent) for name in ("parent", "standard"))
    monkeypatch.setenv("PATH", parent_path)
    monkeypatch.setenv("PWD", str(tmp_path.parent))
    resolved = str(tools["configured"])
    environment = process.child_env(str(tmp_path), resolved)
    expected_path = os.pathsep.join(str(tools[name].parent) for name in (
        "configured", "other-standard", "parent", "standard",
    ))
    assert environment["PATH"] == expected_path
    assert environment["PWD"] == str(tmp_path)
    assert os.environ["PATH"] == parent_path
    assert os.environ["PWD"] == str(tmp_path.parent)

    which = Mock(return_value=resolved)
    monkeypatch.setattr(preparation.shutil, "which", which)
    assert preparation.preflight(["p4"], p4_executable=resolved) == {"status": "ready"}
    which.assert_called_once_with("p4", path=expected_path)


@pytest.mark.parametrize("command", [["p4", "-V"], '"p4" -V'])
def test_windows_initial_command_uses_child_search_path(tools, monkeypatch, command):
    which = Mock(return_value=str(tools["standard"]))
    monkeypatch.setattr(process.shutil, "which", which)
    path = process.tool_search_path()
    assert windows_executable(command, path) == str(tools["standard"])
    which.assert_called_once_with("p4", path=path)
