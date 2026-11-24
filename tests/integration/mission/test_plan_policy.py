"""Opt-in direct checks use fictional tools and fail closed to the verifier."""

import copy
import os
import subprocess
import sys

import pytest

from chef.verification import plan as verification_plan
from chef.verification import plan_policy, runner


@pytest.fixture
def trusted_check(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sample.txt").write_text("fictional input\n", encoding="utf-8")
    (tmp_path / "tools" / "lint.py").write_text("print('fictional lint passed')\n", encoding="utf-8")
    executable = tmp_path / ("python-fictional.exe" if os.name == "nt" else "python-fictional")
    executable.write_bytes(b"fictional executable identity; never launched")
    executable.chmod(0o700)
    command = {
        "id": "lint", "argv": [str(executable), "tools/lint.py", "--check"], "cwd": ".",
        "reusable": True, "inputs": ["src/sample.txt", "tools/lint.py"], "outputs": [],
    }
    plan = {
        "grade": "doc-only", "inputs": ["src/sample.txt", "tools/lint.py"],
        "environment": {"target": "fictional-local"}, "commands": [command], "results": [],
    }
    spec = {
        "argv": list(command["argv"]), "cwd": ".",
        "tools": verification_plan.command_runtime(tmp_path, command)["tools"],
    }
    settings = {"grades": ["doc-only"], "coordinatorChecks": [{"grade": "doc-only", "commands": [spec]}]}
    return tmp_path, plan, settings


def reason_codes(result):
    return {item["code"] for item in result["reasons"]}


def test_explicit_pinned_lint_is_eligible_without_execution_or_writes(trusted_check, monkeypatch):
    root, plan, settings = trusted_check
    original = copy.deepcopy((plan, settings))
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def unexpected(*args, **kwargs):
        pytest.fail("classification must not launch a process")

    monkeypatch.setattr(subprocess, "run", unexpected)
    expected = {"eligible": True, "route": "coordinator", "reasons": []}
    assert verification_plan.coordinator_eligibility(root, plan, settings) == expected
    assert verification_plan.require_coordinator_eligible(root, plan, settings) == expected
    checked = verification_plan.preflight(root, plan, lambda command: None, settings)
    assert checked["status"] == "pass" and checked["warnings"] == []
    assert checked["executionPolicy"] == expected
    assert (plan, settings) == original
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_legacy_plan_still_passes_with_structured_advisories(trusted_check):
    root, plan, _ = trusted_check
    command = plan["commands"][0]
    for field in ("reusable", "inputs", "outputs"):
        del command[field]
    checked = verification_plan.preflight(root, plan, lambda command: None)
    assert checked["status"] == "pass"
    assert checked["executionPolicy"]["route"] == "verifier"
    assert {warning["code"] for warning in checked["warnings"]} == {
        "reuse_policy_missing", "command_inputs_missing", "command_outputs_missing",
    }
    assert all(warning["commandIndex"] == 0 and warning["field"] for warning in checked["warnings"])


@pytest.mark.parametrize("timeout", [1, 3600])
def test_deadline_neither_estimates_duration_nor_grants_trust(trusted_check, timeout):
    root, plan, settings = trusted_check
    plan["commands"][0]["timeoutSeconds"] = timeout
    assert verification_plan.coordinator_eligibility(root, plan, settings)["eligible"]
    assert verification_plan.coordinator_eligibility(root, plan)["route"] == "verifier"


@pytest.mark.parametrize("argv", [
    ["sh", "-c", "echo fictional"],
    ["pwsh", "-Command", "Write-Output fictional"],
    [sys.executable, "tools/build.py"],
    [sys.executable, "-m", "pytest", "-q"],
    [sys.executable, "tools/lint.py", "--fix"],
])
def test_untrusted_shell_build_test_or_altered_argv_stays_with_verifier(trusted_check, argv):
    root, plan, settings = trusted_check
    (root / "tools" / "build.py").write_text("print('fictional build')\n", encoding="utf-8")
    plan["commands"][0].update(argv=argv, timeoutSeconds=1, outputs=[])
    checked = verification_plan.coordinator_eligibility(root, plan, settings)
    assert not checked["eligible"] and checked["route"] == "verifier"


def test_edited_script_and_replaced_executable_invalidate_trust(trusted_check, monkeypatch):
    root, plan, settings = trusted_check
    script = root / "tools" / "lint.py"
    script.write_text("print('changed fictional tool')\n", encoding="utf-8")
    assert reason_codes(verification_plan.coordinator_eligibility(root, plan, settings)) == {"command_identity_mismatch"}
    settings["coordinatorChecks"][0]["commands"][0]["tools"] = verification_plan.command_runtime(root, plan["commands"][0])["tools"]
    original = verification_plan.command_runtime

    def replaced(project, command):
        runtime = original(project, command)
        runtime["tools"][runtime["executable"]] = "0" * 64
        return runtime

    monkeypatch.setattr(verification_plan, "command_runtime", replaced)
    assert reason_codes(verification_plan.coordinator_eligibility(root, plan, settings)) == {"command_identity_mismatch"}


@pytest.mark.parametrize("change", ["append", "remove", "cwd", "order", "split-policy"])
def test_allowlist_matches_entire_ordered_plan(trusted_check, change):
    root, plan, settings = trusted_check
    if change == "remove":
        settings["coordinatorChecks"][0]["commands"].append(copy.deepcopy(settings["coordinatorChecks"][0]["commands"][0]))
    elif change == "cwd":
        plan["commands"][0]["cwd"] = "src"
    else:
        second = copy.deepcopy(plan["commands"][0])
        second.update(id="second", argv=[second["argv"][0], "tools/lint.py", "--strict"])
        plan["commands"].append(second)
        if change in ("order", "split-policy"):
            spec = copy.deepcopy(settings["coordinatorChecks"][0]["commands"][0])
            spec["argv"] = list(second["argv"])
            if change == "order":
                settings["coordinatorChecks"][0]["commands"].insert(0, spec)
            else:
                settings["coordinatorChecks"].append({"grade": "doc-only", "commands": [spec]})
    checked = verification_plan.coordinator_eligibility(root, plan, settings)
    assert checked["route"] == "verifier"
    assert reason_codes(checked) <= {"command_identity_mismatch", "launcher_unpinned"}


def test_matching_full_multicommand_plan_is_eligible(trusted_check):
    root, plan, settings = trusted_check
    second = copy.deepcopy(plan["commands"][0])
    second.update(id="second", argv=[second["argv"][0], "tools/lint.py", "--strict"])
    plan["commands"].append(second)
    spec = copy.deepcopy(settings["coordinatorChecks"][0]["commands"][0])
    spec["argv"] = list(second["argv"])
    settings["coordinatorChecks"][0]["commands"].append(spec)
    assert verification_plan.coordinator_eligibility(root, plan, settings)["eligible"]


@pytest.mark.parametrize("change", [
    "no-grades", "wrong-grade", "missing-tools", "missing-script-pin", "changed-extra-pin", "bad-pin",
    "policy-option", "command-option", "empty-policy", "wrong-policy-type", "missing-cwd",
])
def test_missing_or_unsupported_configuration_defaults_to_verifier(trusted_check, change):
    root, plan, settings = trusted_check
    spec = settings["coordinatorChecks"][0]["commands"][0]
    if change == "no-grades":
        del settings["grades"]
    elif change == "wrong-grade":
        settings["grades"] = ["compile-only"]
    elif change == "missing-tools":
        del spec["tools"]
    elif change == "missing-script-pin":
        del spec["tools"][str((root / "tools" / "lint.py").resolve())]
    elif change == "changed-extra-pin":
        spec["tools"]["src/sample.txt"] = "0" * 64
    elif change == "bad-pin":
        spec["tools"][sys.executable] = "not-a-sha256"
    elif change == "policy-option":
        settings["coordinatorChecks"][0]["allowShell"] = True
    elif change == "command-option":
        spec["timeoutSeconds"] = 1
    elif change == "empty-policy":
        settings["coordinatorChecks"] = []
    elif change == "wrong-policy-type":
        settings["coordinatorChecks"] = {"grade": "doc-only"}
    else:
        del spec["cwd"]
    assert verification_plan.coordinator_eligibility(root, plan, settings)["route"] == "verifier"


def test_relative_tool_pin_and_resolved_cwd_match(trusted_check):
    root, plan, settings = trusted_check
    spec = settings["coordinatorChecks"][0]["commands"][0]
    script_hash = spec["tools"].pop(str((root / "tools" / "lint.py").resolve()))
    spec["tools"]["tools/lint.py"] = script_hash
    spec["cwd"] = "src/.."
    assert verification_plan.coordinator_eligibility(root, plan, settings)["eligible"]


def test_additional_pinned_dependency_changes_invalidate_trust(trusted_check):
    root, plan, settings = trusted_check
    helper = root / "tools" / "helper.py"
    helper.write_text("value = 'fictional helper'\n", encoding="utf-8")
    settings["coordinatorChecks"][0]["commands"][0]["tools"]["tools/helper.py"] = verification_plan.file_hash(helper)
    assert verification_plan.coordinator_eligibility(root, plan, settings)["eligible"]
    helper.write_text("value = 'changed fictional helper'\n", encoding="utf-8")
    assert verification_plan.coordinator_eligibility(root, plan, settings)["route"] == "verifier"
    helper.unlink()
    assert verification_plan.coordinator_eligibility(root, plan, settings)["route"] == "verifier"


def test_policy_and_dependency_changes_reject_receipt_and_prevent_reuse(trusted_check, monkeypatch):
    root, plan, settings = trusted_check
    helper = root / "tools" / "helper.py"
    helper.write_text("value = 'fictional helper'\n", encoding="utf-8")
    pins = settings["coordinatorChecks"][0]["commands"][0]["tools"]
    pins["tools/helper.py"] = verification_plan.file_hash(helper)
    assert "tools/helper.py" not in plan["inputs"]
    executions = []

    def execute(project, command, identity, log):
        executions.append(command["id"])
        log.write(b"fictional check completed\n")
        return subprocess.CompletedProcess(command["argv"], 0)

    monkeypatch.setattr(runner, "execute_command", execute)
    folder = root / "mission"
    folder.mkdir()
    _, receipt, reused = runner.run(root, folder, "M1", plan, lambda command: None, settings=settings)
    assert receipt["status"] == "pass" and not reused
    runner.validate_receipt(root, folder, "M1", receipt, settings=settings)
    original_runtime = verification_plan.runtime_key(root, plan, settings)
    helper.write_text("value = 'changed fictional helper'\n", encoding="utf-8")
    assert verification_plan.runtime_key(root, plan, settings) != original_runtime
    with pytest.raises(verification_plan.VerificationError):
        runner.validate_receipt(root, folder, "M1", receipt, settings=settings)
    pins["tools/helper.py"] = verification_plan.file_hash(helper)
    assert verification_plan.coordinator_eligibility(root, plan, settings)["eligible"]
    with pytest.raises(verification_plan.VerificationError):
        runner.validate_receipt(root, folder, "M1", receipt, settings=settings)
    _, fresh, reused = runner.run(root, folder, "M1", plan, lambda command: None, settings=settings)
    assert fresh["status"] == "pass" and not reused and executions == ["lint", "lint"]


def test_missing_policy_preserves_existing_runtime_policy_identity(trusted_check):
    root, plan, _ = trusted_check
    assert verification_plan.policy_settings(root, plan) == {}
    assert verification_plan.policy_settings(root, plan, {"grades": ["doc-only"]}) == {}
    assert verification_plan.policy_runtime(root, plan) == {}


def test_unreadable_trusted_tool_fails_closed(trusted_check, monkeypatch):
    root, plan, settings = trusted_check

    def unreadable(path):
        raise OSError("fictional unreadable tool")

    monkeypatch.setattr(plan_policy, "file_hash", unreadable)
    assert verification_plan.coordinator_eligibility(root, plan, settings)["route"] == "verifier"


@pytest.mark.parametrize("arguments", [["-m", "fictional_lint"], ["-c", "print('fictional')"]])
def test_even_allowlisted_scriptless_interpreter_stays_with_verifier(trusted_check, arguments):
    root, plan, settings = trusted_check
    command = plan["commands"][0]
    command["argv"] = [command["argv"][0], *arguments]
    spec = settings["coordinatorChecks"][0]["commands"][0]
    spec.update(argv=command["argv"], tools=verification_plan.command_runtime(root, command)["tools"])
    assert reason_codes(verification_plan.coordinator_eligibility(root, plan, settings)) == {"launcher_unpinned"}


@pytest.mark.parametrize("name,arguments", [("fictional.cmd", []), ("fictional.bat", [])])
def test_even_allowlisted_shell_inline_and_batch_wrappers_stay_with_verifier(trusted_check, name, arguments):
    root, plan, settings = trusted_check
    executable = root / name
    executable.write_bytes(b"fictional launcher; never launched")
    executable.chmod(0o700)
    command = plan["commands"][0]
    command["argv"] = [str(executable), *arguments]
    spec = settings["coordinatorChecks"][0]["commands"][0]
    spec.update(argv=command["argv"], tools=verification_plan.command_runtime(root, command)["tools"])
    assert reason_codes(verification_plan.coordinator_eligibility(root, plan, settings)) == {"launcher_unpinned"}


@pytest.mark.parametrize("name,options", [
    ("bash", ["-lc"]), ("bash", ["-cl"]), ("bash", ["-o", "nounset", "-c"]),
    ("BASH.exe", ["-lc"]), ("sh", ["-c"]), ("zsh", ["-c"]), ("dash", ["-c"]),
    ("ash", ["-c"]), ("ksh", ["-c"]), ("csh", ["-c"]), ("tcsh", ["-c"]), ("fish", ["-c"]),
    ("pwsh", ["-c"]), ("pwsh", ["-Co"]), ("pwsh", ["-CommandWithArgs"]), ("pwsh", ["-File"]),
    ("powershell", ["-C"]), ("powershell", ["-Command"]),
    ("powershell.exe", ["-NoProfile", "-File"]), ("cmd", ["/c"]),
])
def test_allowlisted_shells_cannot_gain_direct_route_from_existing_file_argument(trusted_check, monkeypatch, name, options):
    root, plan, settings = trusted_check
    executable = root / name
    executable.write_bytes(b"fictional shell identity; never launched")
    executable.chmod(0o700)
    command = plan["commands"][0]
    command["argv"] = [str(executable), *options, "tools/lint.py"]
    runtime = verification_plan.command_runtime(root, command)
    if (name, options) in [("bash", ["-lc"]), ("pwsh", ["-c"]), ("pwsh", ["-File"])]:
        assert len(runtime["tools"]) == 2
    spec = settings["coordinatorChecks"][0]["commands"][0]
    spec.update(argv=command["argv"], tools=runtime["tools"])

    def unexpected(*args, **kwargs):
        pytest.fail("shell preflight must not execute a command")

    monkeypatch.setattr(subprocess, "run", unexpected)
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    checked = verification_plan.preflight(root, plan, lambda command: None, settings)
    assert checked["status"] == "pass"
    assert checked["executionPolicy"]["route"] == "verifier"
    assert reason_codes(checked["executionPolicy"]) == {"shell_requires_verifier"}
    with pytest.raises(verification_plan.VerificationError) as error:
        verification_plan.require_coordinator_eligible(root, plan, settings)
    assert error.value.category == "coordinator_ineligible"
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_pinned_native_checker_keeps_direct_route(trusted_check):
    root, plan, settings = trusted_check
    executable = root / "fictional-lint.exe"
    executable.write_bytes(b"fictional native checker identity; never launched")
    executable.chmod(0o700)
    command = plan["commands"][0]
    command["argv"] = [str(executable), "--check"]
    spec = settings["coordinatorChecks"][0]["commands"][0]
    spec.update(argv=command["argv"], tools=verification_plan.command_runtime(root, command)["tools"])
    assert verification_plan.require_coordinator_eligible(root, plan, settings)["route"] == "coordinator"


@pytest.mark.parametrize("preparation", ["readiness", "resources"])
def test_preparation_keeps_verifier_route(trusted_check, preparation):
    root, plan, settings = trusted_check
    if preparation == "readiness":
        settings["readiness"] = {"doc-only": [{"argv": [sys.executable, "-c", "pass"]}]}
    else:
        settings.update(resourceDir="locks", resources={"doc-only": ["fictional-resource"]})
    checked = verification_plan.preflight(root, plan, lambda command: None, settings)
    assert checked["status"] == "pass"
    assert reason_codes(checked["executionPolicy"]) == {"preparation_required"}
    assert not (root / "locks").exists()


@pytest.mark.parametrize("field", ["reusable", "inputs", "outputs"])
def test_direct_execution_requires_explicit_step_declarations(trusted_check, field):
    root, plan, settings = trusted_check
    if field == "outputs":
        plan["commands"][0]["reusable"] = False
    del plan["commands"][0][field]
    assert verification_plan.coordinator_eligibility(root, plan, settings)["route"] == "verifier"


def test_explicit_nonreusable_readonly_check_is_supported(trusted_check):
    root, plan, settings = trusted_check
    plan["commands"][0]["reusable"] = False
    assert verification_plan.coordinator_eligibility(root, plan, settings)["eligible"]


@pytest.mark.parametrize("scope", ["plan-inputs", "command-inputs", "command-outputs"])
def test_whole_project_scopes_are_advisory_and_not_direct(trusted_check, scope):
    root, plan, settings = trusted_check
    plan["inputs"] = ["."]
    if scope == "command-inputs":
        plan["commands"][0]["inputs"] = ["**/*"]
    elif scope == "command-outputs":
        plan["commands"][0]["outputs"] = ["."]
    checked = verification_plan.preflight(root, plan, lambda command: None, settings)
    assert checked["status"] == "pass" and checked["warnings"]
    assert checked["executionPolicy"]["route"] == "verifier"


def test_nonempty_outputs_and_result_artifacts_keep_verifier(trusted_check):
    root, plan, settings = trusted_check
    plan["commands"][0]["outputs"] = ["lint-result.json"]
    assert reason_codes(verification_plan.coordinator_eligibility(root, plan, settings)) == {"outputs_declared"}
    plan["results"] = [{"path": "lint-result.json", "command": "lint", "equals": {"status": "passed"}}]
    assert reason_codes(verification_plan.coordinator_eligibility(root, plan, settings)) == {"result_artifacts"}


def test_eligibility_does_not_bypass_existing_plan_grade_result_validation(trusted_check):
    root, plan, settings = trusted_check
    plan["grade"] = "editmode-pass"
    settings["grades"].append("editmode-pass")
    settings["coordinatorChecks"][0]["grade"] = "editmode-pass"
    checked = verification_plan.preflight(root, plan, lambda command: None, settings)
    assert checked["status"] == "fail" and checked["failure"]["category"] == "plan_invalid"
    with pytest.raises(verification_plan.VerificationError) as error:
        verification_plan.require_coordinator_eligible(root, plan, settings)
    assert error.value.category == "coordinator_ineligible"


def test_allowlist_does_not_bypass_guard(trusted_check):
    root, plan, settings = trusted_check

    def guard(command):
        raise RuntimeError("fictional guard denied this command")

    checked = verification_plan.preflight(root, plan, guard, settings)
    assert checked["status"] == "fail" and checked["failure"]["category"] == "guard_rejected"
    assert not checked["executionPolicy"]["eligible"]


def test_invalid_optional_policy_does_not_break_old_verifier_plan(trusted_check):
    root, plan, settings = trusted_check
    settings["coordinatorChecks"] = {"unsupported": True}
    checked = verification_plan.preflight(root, plan, lambda command: None, settings)
    assert checked["status"] == "pass"
    assert reason_codes(checked["executionPolicy"]) == {"policy_invalid"}
