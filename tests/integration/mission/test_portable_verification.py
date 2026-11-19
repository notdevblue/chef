"""Portable process, path, and policy behavior with fictional local inputs."""

import base64
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from chef.dashboard.models import raw_url
from chef.guards import mission
from chef.mission import state, supervisor, usage
from chef.verification import plan, runner


def adapter():
    return {"guard": {"deny": [{"pattern": "^dangerous-operation$", "reason": "fictional blocked operation"}]}}


def test_verification_argv_data_is_not_reinterpreted_as_shell(tmp_path):
    context = SimpleNamespace(adapter=adapter(), project=str(tmp_path))
    command = {"argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(dangerous-operation)"], "cwd": "."}
    identity = plan.guarded_runtime(str(tmp_path), command, state.verification_guard(context))
    with (tmp_path / "result.log").open("wb") as log:
        result = runner.execute_command(str(tmp_path), command, identity, log)
    assert result.returncode == 0
    assert (tmp_path / "result.log").read_text(encoding="utf-8").strip() == "$(dangerous-operation)"


@pytest.mark.parametrize("argv", [
    ["p4", "submit"],
    ["p4", "-p", 'fictional-host"quoted', "submit"],
    ["env", "LABEL=literal;data", "p4", "submit"],
    ["env", "-u", "LABEL", "p4", "submit"],
    ["sudo", "-u", "fictional-user", "p4", "submit"],
    ["nohup", "p4", "submit"],
    ["timeout", "-s", "TERM", "5", "p4", "submit"],
    ["xargs", "-n", "1", "p4", "submit"],
    ["bash", "-o", "nounset", "-c", "p4 submit"],
    ["bash", "-c", "--", "p4 submit"],
    ["bash", "+c", "p4 submit"],
    ["bash", "-lc", "--", "p4 submit"],
    ["sh", "-c", "--", "p4 submit"],
    ["bash", "-c", "-x", "p4 submit"],
    ["bash", "-c", "+x", "p4 submit"],
    ["bash", "-c", "-o", "nounset", "p4 submit"],
    ["bash", "-co", "nounset", "p4 submit"],
    ["bash", "-oc", "nounset", "p4 submit"],
    ["env", "bash", "-lc", "p4 submit"],
    ["pwsh", "-Command", "p4 submit"],
    ["pwsh", "-EncodedCommand", base64.b64encode("p4 submit".encode("utf-16-le")).decode("ascii")],
    ["python3", "p4c.py", "--client", "fictional-client", "submit"],
    [sys.executable, "--check-hash-based-pycs", "never", "p4c.py", "submit"],
])
def test_structured_argv_preserves_p4_policy_through_explicit_wrappers(argv):
    policy = {"guard": {"p4Deny": [{"commands": ["submit"], "reason": "fictional submit restriction"}]}}
    assert mission.judge_argv(policy, argv).kind == "rule"


def test_direct_p4_argv_still_checks_client_even_when_allow_pattern_matches(tmp_path):
    (tmp_path / ".p4config").write_text("P4CLIENT=fictional-garden\n", encoding="utf-8")
    policy = {"guard": {"allow": [".*"], "p4Client": {"require": True}}}
    assert mission.judge_argv(policy, ["p4", "edit", "source.py"], project=str(tmp_path)).kind == "rule"
    assert mission.judge_argv(policy, ["p4", "-c", "fictional-other", "edit", "source.py"], project=str(tmp_path)).kind == "rule"
    assert mission.judge_argv(policy, ["p4", "-c", "fictional-garden", "edit", "source.py"], project=str(tmp_path)).kind == "allow"
    nested = tmp_path / "nested"
    nested.mkdir()
    context = SimpleNamespace(adapter=policy, project=str(tmp_path))
    with pytest.raises(state.StateError, match="fictional-other"):
        state.verification_guard(context).argv(["p4", "-c", "fictional-other", "edit", "source.py"], str(nested))
    for literal in ("fictional-$other", "fictional-{other}"):
        assert mission.judge_argv(policy, ["p4", "-c", literal, "edit", "source.py"], project=str(tmp_path)).kind == "rule"


def test_p4c_explicit_project_cannot_redirect_protected_operations(tmp_path):
    own, foreign = tmp_path / "own", tmp_path / "foreign"
    for folder in (own, foreign):
        folder.mkdir()
        (folder / ".p4config").write_text("P4CLIENT=fictional-" + folder.name + "\n", encoding="utf-8")
    policy = {"guard": {"allow": [".*"], "p4Client": {"require": True}}}
    argv = ["python3", "p4c.py", "--project", str(foreign), "sync"]
    assert mission.judge_argv(policy, argv, project=str(own)).kind == "rule"
    from chef.guards.argv import render
    assert mission.judge_command(policy, render(argv), "Bash", project=str(own)).kind == "rule"
    argv[3] = str(own)
    assert mission.judge_argv(policy, argv, project=str(own)).kind == "allow"
    nested = own / "nested"
    nested.mkdir()
    (nested / ".p4config").write_text("P4CLIENT=fictional-nested\n", encoding="utf-8")
    context = SimpleNamespace(adapter=policy, project=str(own))
    with pytest.raises(state.StateError, match="cwd"):
        state.verification_guard(context).argv(["python3", "p4c.py", "sync"], str(nested))
    state.verification_guard(context).argv(argv, str(nested))
    assert mission.judge_command(policy, "python3 p4c.py sync", "Bash", project=str(own), cwd=str(nested)).kind == "rule"


@pytest.mark.parametrize("argv", [["env", "-C", "foreign", "p4", "sync"], ["sudo", "-D", "foreign", "p4", "sync"]])
def test_scope_changing_wrappers_fail_closed_in_both_guard_paths(argv):
    from chef.guards.argv import render

    assert mission.judge_argv({}, argv).kind == "rule"
    assert mission.judge_command({}, render(argv), "Bash").kind == "rule"


def test_regex_case_option_preserves_default_and_allows_explicit_case_sensitive_paths():
    rule = {"pattern": "^python Trusted/run.py$"}
    policy = {"guard": {"allow": [rule], "deny": [{"pattern": "^python", "reason": "restricted"}]}}
    assert mission.judge_argv(policy, ["python", "trusted/run.py"]).kind == "allow"
    rule["caseSensitive"] = True
    assert mission.judge_argv(policy, ["python", "Trusted/run.py"]).kind == "allow"
    assert mission.judge_argv(policy, ["python", "trusted/run.py"]).kind == "rule"
    rule["caseSensitive"] = "invalid"
    problems = []
    mission.judge_argv(policy, ["python", "trusted/run.py"], problems)
    assert problems and "caseSensitive" in problems[0]


def test_explicit_shell_script_is_inspected_but_its_positional_data_is_not():
    assert mission.judge_argv(adapter(), ["bash", "-c", "dangerous-operation"]).kind == "rule"
    assert mission.judge_argv(adapter(), ["bash", "-c", 'printf "%s" "$1"', "shell", "$(dangerous-operation)"]).kind == "allow"
    assert mission.judge_argv(adapter(), ["pwsh", "-CommandWithArgs", "Write-Output $args[0]", "$(dangerous-operation)"]).kind == "allow"


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "dash", "ksh"])
@pytest.mark.parametrize("options", [["-c", "--"], ["-lc", "--"], ["-c", "-x"], ["-c", "-o", "nounset"], ["+c"]])
def test_shell_command_operand_matches_actual_local_option_parsing(shell, options, tmp_path):
    from chef.guards.argv import describe

    executable = shutil.which(shell)
    if not executable:
        pytest.skip("shell unavailable")
    script = "printf CHEF_SCRIPT_PROBE"
    argv = [executable, *options, script]
    assert describe(argv).script == script
    home = tmp_path / "home"
    home.mkdir()
    environment = {key: value for key, value in os.environ.items() if key not in ("BASH_ENV", "ENV")}
    environment.update(HOME=str(home), ZDOTDIR=str(home), PWD=str(tmp_path))
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=environment, cwd=tmp_path)
    assert result.returncode == 0 and result.stdout == "CHEF_SCRIPT_PROBE"


def test_shell_option_delimiter_is_consumed_only_once():
    from chef.guards.argv import describe

    assert describe(["bash", "-c", "--", "--", "p4 submit"]).script == "--"


def test_command_child_environment_uses_actual_cwd_without_global_chdir(tmp_path, monkeypatch):
    directory = tmp_path / "nested"
    directory.mkdir()
    monkeypatch.setenv("PWD", str(tmp_path / "unrelated"))
    previous = os.getcwd()
    command = {"argv": [sys.executable, "-c", "import json,os; print(json.dumps([os.getcwd(),os.environ['PWD']]))"], "cwd": "nested"}
    with (tmp_path / "cwd.log").open("wb") as log:
        result = runner.execute_command(str(tmp_path), command, {"executable": sys.executable}, log)
    actual, environment = json.loads((tmp_path / "cwd.log").read_text(encoding="utf-8"))
    assert result.returncode == 0 and os.path.samefile(actual, environment)
    assert os.path.samefile(actual, directory)
    assert os.getcwd() == previous and os.environ["PWD"] == str(tmp_path / "unrelated")


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal backslash filename")
def test_input_snapshot_and_dashboard_raw_url_preserve_literal_backslash(tmp_path):
    target = tmp_path / "data\\leaf.txt"
    target.write_text("fictional input", encoding="utf-8")
    files = plan.snapshot(str(tmp_path), [target.name])
    command = {"inputs": [target.name]}
    assert plan.step_inputs(str(tmp_path), command, files) == files
    from chef.dashboard.contracts import RAW_PREFIX
    assert unquote(raw_url(str(target))[len(RAW_PREFIX):]) == str(target)


def test_supervisor_detects_alias_and_filesystem_case_overlap(tmp_path):
    project = tmp_path / "Garden"
    project.mkdir()
    alias = tmp_path / "shortcut"
    try:
        alias.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    assert supervisor.overlaps(str(project), str(alias))
    assert supervisor.overlaps(str(project), str(alias / "future"))
    spelling = tmp_path / "garden"
    assert supervisor.overlaps(str(project), str(spelling)) == spelling.exists()


def test_usage_report_accepts_distinct_case_projects_and_rejects_aliases(tmp_path):
    first, second = tmp_path / "Garden", tmp_path / "garden"
    first.mkdir()
    if second.exists():
        pytest.skip("volume does not distinguish path case")
    second.mkdir()
    report = tmp_path / "report.json"
    args = ["--project", f"first={first}", "--project", f"second={second}",
            "--claude-home", str(tmp_path / "empty-home"), "--start", "2026-01-01T00:00:00Z",
            "--end", "2026-01-02T00:00:00Z", "--output", str(report)]
    assert usage.main(args) == 0 and report.is_file()
    args[3] = f"second={first}"
    with pytest.raises(SystemExit):
        usage.main(args)
