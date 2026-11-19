"""Portable preparation arguments reach a real subprocess without shell parsing."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

from chef.infrastructure.errors import UsageError
from chef.slots.configuration import Config
from chef.slots.execution import Runner


@pytest.fixture
def config(tmp_path):
    main = tmp_path / "orchard"
    main.mkdir()
    adapter = {
        "project": "Orchard",
        "paths": {"stateDir": "state"},
        "vars": {"python": sys.executable},
        "slots": {
            "enabled": True,
            "root": str(tmp_path / "plots"),
            "client": "orchard-{name}",
            "names": ["north"],
        },
    }
    return Config(SimpleNamespace(project=str(main)), adapter)


@pytest.mark.parametrize("before_run", [False, True])
def test_vector_arguments_preserve_data_and_working_directory(config, tmp_path, before_run):
    output = tmp_path / "arguments.json"
    helper = tmp_path / "capture.py"
    helper.write_text(
        "import json, os, sys\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as stream:\n"
        "    json.dump([sys.argv[2:], os.getcwd(), os.environ['PWD']], stream)\n",
        encoding="utf-8",
    )
    expected = ["a b", "한글", 'a"b', "$HOME", "x;y", "", "trailing\\"]
    entry = {"id": "capture", "argv": ["{python}", str(helper), str(output), *expected], "cwd": "{main}"}
    if before_run:
        config.before_run = [entry]
        steps = config.before_run_steps("north", "new", "session-1")
    else:
        config.prepare = [entry]
        steps = config.build_steps("north")
    assert Runner(config).execute(steps[0]) == 0
    args, cwd, pwd = json.loads(output.read_text(encoding="utf-8"))
    assert args == expected
    assert os.path.samefile(cwd, config.main)
    assert os.path.samefile(pwd, config.main)


def test_vector_expands_each_argument_and_baseline_without_splitting(config, monkeypatch):
    calls = []
    monkeypatch.setattr(config.vcs, "baseline", lambda: calls.append(1) or "123")
    config.vars["message"] = "two words"
    config.prepare = [{"id": "sync", "argv": ["tool", "{message}", "@{baseline}"]}]
    assert config.build_steps("north")[0].command == ["tool", "two words", "@123"]
    assert config.build_steps("north")[0].command == ["tool", "two words", "@123"]
    assert calls == [1]


@pytest.mark.parametrize("entry", [
    {"id": "invalid", "run": "tool", "argv": ["tool"]},
    {"id": "invalid", "argv": []},
    {"id": "invalid", "argv": "tool --flag"},
    {"id": "invalid", "argv": [""]},
    {"id": "invalid", "argv": ["tool", 1]},
    {"id": "invalid", "argv": ["tool", "bad\x00argument"]},
])
def test_invalid_vector_configuration_is_rejected_before_execution(config, entry):
    config.prepare = [entry]
    with pytest.raises(UsageError, match="slots.prepare"):
        config.build_steps("north")


def test_undefined_argument_variable_is_rejected(config):
    config.prepare = [{"id": "invalid", "argv": ["tool", "{undefined}"]}]
    with pytest.raises(UsageError, match="undefined"):
        config.build_steps("north")


@pytest.mark.parametrize("value", ["", "bad\x00executable"])
def test_invalid_expanded_executable_is_rejected_before_execution(config, value):
    config.vars["tool"] = value
    config.prepare = [{"id": "invalid", "argv": ["{tool}", "argument"]}]
    with pytest.raises(UsageError, match="argv"):
        config.build_steps("north")


def test_legacy_command_remains_a_string(config):
    config.prepare = [{"id": "legacy", "run": 'tool "{main}"'}]
    assert config.build_steps("north")[0].command == f'tool "{config.main}"'


def test_slot_path_allows_siblings_and_refuses_actual_main_overlap(config, tmp_path):
    config.root = str(tmp_path)
    assert not config.path_problem("north")
    config.main = str(tmp_path / "north" / "source")
    assert "겹침" in config.path_problem("north")
    config.main = str(tmp_path)
    assert "겹침" in config.path_problem("north")


def test_slot_path_refuses_overlap_through_a_parent_alias(config, tmp_path):
    alias = tmp_path / "shortcut"
    try:
        alias.symlink_to(config.main, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    config.root = str(alias / "plots")
    assert "겹침" in config.path_problem("north")
