import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import mission_state
import verification


def resource_settings(tmp_path, *resources):
    return {"resourceDir": str(tmp_path / "shared-resources"), "resources": {"pytest-pass": list(resources)}}


def allow_command(command):
    return command


def test_preflight_is_read_only_and_checks_readiness_without_running(mission, monkeypatch):
    root, folder, plan = mission
    settings = resource_settings(root.parent, "engine")
    settings["readiness"] = {"pytest-pass": [{"argv": [sys.executable, "-c", "print('{project}')"]}]}
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    def unexpected(*args, **kwargs):
        pytest.fail("preflight started a subprocess")
    monkeypatch.setattr(verification.subprocess, "run", unexpected)
    result = verification.preflight(root, plan, allow_command, settings)
    assert json.loads(json.dumps(result)) == result
    assert result == {"status": "pass", "grade": "pytest-pass", "commandCount": 1,
                      "readinessCount": 1, "resources": ["engine"], "failure": {}}
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == before
    assert not (folder / "verification").exists()
    assert not Path(settings["resourceDir"]).exists()


@pytest.mark.parametrize("change,category", [("cwd", "preflight"), ("executable", "preflight"),
                                              ("control", "preflight"), ("guard", "guard_rejected"),
                                              ("grade", "plan_invalid")])
def test_preflight_classifies_invalid_plan_and_readiness(mission, change, category):
    root, folder, plan = mission
    settings = {"grades": ["pytest-pass"], "readiness": {"pytest-pass": [{"argv": [sys.executable, "-c", "pass"]}]}}
    command = settings["readiness"]["pytest-pass"][0]
    if change == "cwd":
        command["cwd"] = "missing"
    elif change == "executable":
        command["argv"][0] = "fictional-missing-program"
    elif change == "control":
        command["argv"].append("invalid\nargument")
    elif change == "grade":
        settings["grades"] = []
    else:
        command["argv"].append("forbidden")
    def guard(command):
        if "forbidden" in command:
            raise RuntimeError("fictional guard rejection")
    result = verification.preflight(root, plan, guard, settings)
    assert result["status"] == "fail" and result["failure"]["category"] == category
    assert not (folder / "verification").exists()


@pytest.mark.parametrize("names", [["../escape"], ["nested/name"], ["nested\\name"], ["a\nb"],
                                  ["."], ["alpha", "alpha"], ["Alpha", "alpha"], ["CON"], ["lpt1"], [1], "alpha"])
def test_preflight_rejects_resource_path_injection_and_duplicate_names(mission, names):
    root, _, plan = mission
    settings = resource_settings(root.parent)
    settings["resources"]["pytest-pass"] = names
    assert verification.preflight(root, plan, allow_command, settings)["status"] == "fail"
    assert not Path(settings["resourceDir"]).exists()


@pytest.mark.parametrize("value", ["..", "invalid\nfolder", 0, ""])
def test_preflight_rejects_invalid_resource_directory(mission, value):
    root, _, plan = mission
    settings = resource_settings(root.parent, "engine")
    settings["resourceDir"] = value
    assert verification.preflight(root, plan, allow_command, settings)["failure"]["category"] == "preflight"


def test_resources_serialize_distinct_projects_and_release_after_run(mission, tmp_path, monkeypatch):
    root, folder, plan = mission
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "src.py").write_text("value = 2\n", encoding="utf-8")
    other_plan = {"grade": "pytest-pass", "inputs": ["src.py"], "environment": {"target": "fictional"},
                  "commands": [{"argv": [sys.executable, "-c", "pass"]}]}
    other_folder = other / "mission"
    other_folder.mkdir()
    settings = resource_settings(tmp_path, "engine")
    entered, finish = threading.Event(), threading.Event()
    real_execute = verification.execute_command
    def launch(project, command, identity, log):
        if project == root:
            entered.set()
            assert finish.wait(10)
        return real_execute(project, command, identity, log)
    monkeypatch.setattr(verification, "execute_command", launch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(verification.run, root, folder, "M1", plan, allow_command, settings=settings)
        try:
            assert entered.wait(10), first.result(timeout=1)
            _, busy, reused = verification.run(other, other_folder, "M1", other_plan, allow_command, settings=settings)
            assert busy["failure"]["category"] == "resource_busy" and not reused
            assert busy["executionCounts"]["executed"] == 0
            assert (Path(settings["resourceDir"]) / "engine.lock").exists()
        finally:
            finish.set()
        assert first.result(timeout=10)[1]["status"] == "pass"
    assert not (Path(settings["resourceDir"]) / "engine.lock").exists()
    assert verification.run(other, other_folder, "M1", other_plan, allow_command, settings=settings)[1]["status"] == "pass"


def test_partial_resource_acquisition_releases_in_sorted_order_without_stale_deletion(mission, tmp_path, monkeypatch):
    root, folder, plan = mission
    settings = resource_settings(tmp_path, "zeta", "alpha")
    directory = Path(settings["resourceDir"])
    directory.mkdir()
    blocker = directory / "zeta.lock"
    blocker.write_text("999999999\tfictional dead holder\n", encoding="utf-8")
    seen = []
    acquire = verification.ResourceLock.acquire
    def track(lock):
        seen.append(Path(lock.path).stem)
        return acquire(lock)
    monkeypatch.setattr(verification.ResourceLock, "acquire", track)
    record = verification.run(root, folder, "M1", plan, allow_command, settings=settings)[1]
    assert record["failure"]["category"] == "resource_busy"
    assert seen == ["alpha", "zeta"]
    assert not (directory / "alpha.lock").exists()
    assert blocker.read_text(encoding="utf-8") == "999999999\tfictional dead holder\n"
    assert not (root / "runs").exists()


@pytest.mark.parametrize("failure,category", [("exit", "readiness_exit"), ("timeout", "readiness_timeout"),
                                             ("launch", "readiness_launch_error")])
def test_readiness_failures_do_not_start_validation_and_release_resources(mission, tmp_path, monkeypatch, failure, category):
    root, folder, plan = mission
    settings = resource_settings(tmp_path, "engine")
    settings["readiness"] = {"pytest-pass": [{"argv": [sys.executable, "-c", "pass"], "timeoutSeconds": 1}]}
    verification.platform.platform()
    def launch(argv, **kwargs):
        assert (Path(settings["resourceDir"]) / "engine.lock").is_file()
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["shell"] is False and kwargs["timeout"] == 1
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if failure == "launch":
            raise OSError("fictional readiness launch failure")
        return subprocess.CompletedProcess(argv, 7)
    monkeypatch.setattr(verification.subprocess, "run", launch)
    record = verification.run(root, folder, "M1", plan, allow_command, settings=settings)[1]
    assert record["failure"]["category"] == category
    assert record["plan"] == plan and record["executionCounts"]["executed"] == 0
    assert all(step["status"] == "not_run" for step in record["steps"])
    assert not (root / "runs").exists()
    assert not (Path(settings["resourceDir"]) / "engine.lock").exists()


def test_readiness_input_mutation_prevents_validation(mission):
    root, folder, plan = mission
    settings = {"readiness": {"pytest-pass": [{"argv": [sys.executable, "-c",
        "from pathlib import Path; Path('src.py').write_text('changed by readiness')"]}]}}
    record = verification.run(root, folder, "M1", plan, allow_command, settings=settings)[1]
    assert record["failure"]["category"] == "input_changed"
    assert record["cacheIntegrity"]["inputsUnchanged"] is False
    assert record["executionCounts"]["executed"] == 0 and not (root / "runs").exists()


def test_readiness_runs_before_receipt_reuse_and_only_substitutes_project(mission, tmp_path):
    root, folder, plan = mission
    settings = resource_settings(tmp_path, "engine")
    settings["readiness"] = {"pytest-pass": [{"argv": [sys.executable, "-c",
        "import sys; from pathlib import Path; p=Path(sys.argv[1])/'ready-runs'; "
        "p.open('a').write('x'); assert sys.argv[2] == '{literal}'",
        "{project}", "{literal}"]}]}
    first = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    second = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    assert first[1]["status"] == "pass" and second[2] is True and first[0] == second[0]
    assert (root / "ready-runs").read_text() == "xx" and (root / "runs").read_text() == "x"
    settings["readiness"]["pytest-pass"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(3)"]
    failed = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    assert failed[1]["failure"]["category"] == "readiness_exit" and failed[0] != first[0]
    assert (root / "runs").read_text() == "x"


@pytest.mark.parametrize("explicit", [False, True])
def test_verification_child_utf8_defaults_preserve_parent_and_explicit_values(mission, monkeypatch, explicit):
    root, folder, plan = mission
    keys = {"PYTHONUTF8": "0", "PYTHONIOENCODING": "latin-1"}
    for key, value in keys.items():
        if explicit:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    plan["commands"] = [{"argv": [sys.executable, "-c",
        "import json,os; from pathlib import Path; "
        "Path('child.json').write_text(json.dumps({k:os.environ[k] for k in ['PYTHONUTF8','PYTHONIOENCODING']}))"]}]
    before = dict(os.environ)
    record = verification.run(root, folder, "M1", plan, allow_command)[1]
    assert record["status"] == "pass" and dict(os.environ) == before
    expected = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if explicit:
        expected = keys
    assert json.loads((root / "child.json").read_text()) == expected


def test_reference_bundle_preflight_and_receipt_reuse_require_current_sources(mission):
    root, folder, plan = mission
    source = root / "reference.txt"
    source.write_text("fictional rule one", encoding="utf-8")
    verification.reference_bundle.snapshot(root, ["reference.txt"], "references.json")
    plan["referenceBundle"] = "references.json"
    first = verification.run(root, folder, "M1", plan, allow_command)
    assert first[1]["status"] == "pass"
    source.write_text("fictional rule two", encoding="utf-8")
    checked = verification.preflight(root, plan, allow_command)
    assert checked["failure"]["category"] == "reference_changed"
    with pytest.raises(verification.VerificationError):
        verification.validate_receipt(root, folder, "M1", first[1])
    verification.reference_bundle.snapshot(root, ["reference.txt"], "references.json",
                                           expected_sha256=verification.file_hash(root / "references.json"))
    second = verification.run(root, folder, "M1", plan, allow_command)
    assert second[1]["status"] == "pass" and second[0] != first[0]
    assert (root / "runs").read_text() == "xx"


def test_reference_bundle_recapture_metadata_invalidates_receipt_and_stage_identity(mission):
    root, folder, plan = mission
    (root / "reference.txt").write_text("fictional rule", encoding="utf-8")
    verification.reference_bundle.snapshot(root, ["reference.txt"], "references.json", revision="first")
    plan["referenceBundle"] = "references.json"
    plan["commands"][0].update(reusable=True, outputs=[])
    first = verification.run(root, folder, "M1", plan, allow_command)
    verification.reference_bundle.snapshot(root, ["reference.txt"], "references.json", revision="second",
                                           expected_sha256=verification.file_hash(root / "references.json"))
    second = verification.run(root, folder, "M1", plan, allow_command)
    assert first[1]["runner"] != second[1]["runner"]
    assert first[1]["steps"][0]["key"] != second[1]["steps"][0]["key"]
    assert second[1]["status"] == "pass" and second[1]["executionCounts"] == {"executed": 1, "reused": 0}


def test_reference_source_change_during_validation_invalidates_run_integrity(mission):
    root, folder, plan = mission
    (root / "reference.txt").write_text("fictional original", encoding="utf-8")
    verification.reference_bundle.snapshot(root, ["reference.txt"], "references.json")
    plan["referenceBundle"] = "references.json"
    plan["commands"] = [{"argv": [sys.executable, "-c",
        "from pathlib import Path; Path('reference.txt').write_text('fictional changed')"]}]
    record = verification.run(root, folder, "M1", plan, allow_command)[1]
    assert record["failure"]["category"] == "runtime_changed"
    assert record["cacheIntegrity"]["runtimeUnchanged"] is False
    assert record["steps"][0]["status"] == "failed"


def test_unexpected_runner_exception_still_releases_resources(mission, tmp_path, monkeypatch):
    root, folder, plan = mission
    settings = resource_settings(tmp_path, "engine")
    def launch(*args, **kwargs):
        raise RuntimeError("fictional unexpected failure")
    monkeypatch.setattr(verification.subprocess, "run", launch)
    with pytest.raises(RuntimeError, match="fictional unexpected failure"):
        verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    assert not (Path(settings["resourceDir"]) / "engine.lock").exists()


def readiness_policy(mission):
    root, _, _ = mission
    (root / "machine-state").write_text("ready", encoding="utf-8")
    (root / "readiness.py").write_text(
        "from pathlib import Path\nraise SystemExit(int(Path('machine-state').read_text() != 'ready'))\n",
        encoding="utf-8",
    )
    settings = resource_settings(root.parent, "engine")
    settings["readiness"] = {"pytest-pass": [{"argv": [sys.executable, "readiness.py"]}]}
    return settings


@pytest.mark.parametrize("change", ["resources", "resourceDir", "argv", "timeout", "script"])
def test_policy_and_readiness_tools_invalidate_receipt_and_stage_reuse(mission, change):
    root, folder, plan = mission
    settings = readiness_policy(mission)
    plan["commands"][0].update(reusable=True, outputs=[])
    first = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    assert first[1]["status"] == "pass"
    if change == "resources":
        settings["resources"]["pytest-pass"] = ["other-engine"]
    elif change == "resourceDir":
        settings["resourceDir"] = str(root.parent / "other-resources")
    elif change == "argv":
        settings["readiness"]["pytest-pass"][0]["argv"].append("extra")
    elif change == "timeout":
        settings["readiness"]["pytest-pass"][0]["timeoutSeconds"] = 20
    else:
        (root / "readiness.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(verification.VerificationError):
        verification.validate_receipt(root, folder, "M1", first[1], settings=settings)
    second = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    assert second[1]["status"] == "pass" and second[2] is False
    assert second[1]["executionCounts"] == {"executed": 1, "reused": 0}
    assert second[1]["runner"] != first[1]["runner"]
    assert second[1]["steps"][0]["key"] != first[1]["steps"][0]["key"]
    assert (root / "runs").read_text() == "xx"


def test_machine_observation_does_not_invalidate_receipt_or_run_readiness_at_review(mission, monkeypatch):
    root, folder, plan = mission
    settings = readiness_policy(mission)
    path, receipt, _ = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    (root / "machine-state").write_text("closed", encoding="utf-8")
    report = root / "review.md"
    report.write_text("Fictional independent review", encoding="utf-8")
    def unexpected(*args, **kwargs):
        pytest.fail("receipt validation reran readiness")
    monkeypatch.setattr(verification, "execute_command", unexpected)
    verification.validate_receipt(root, folder, "M1", receipt, settings=settings)
    verification.validate_receipt(root, folder, "M1", receipt)
    review_path = verification.record_review(root, folder, "M1", path, report, "reviewer", "unit", "clean", settings=settings)
    verification.validate_review(folder, "M1", path, review_path)
    assert verification.review_scope(root, folder, "M1", plan, settings=settings) == []


def test_missing_current_policy_rejects_policy_receipt_but_none_preserves_optional_api(mission):
    root, folder, plan = mission
    settings = readiness_policy(mission)
    _, receipt, _ = verification.run(root, folder, "M1", plan, allow_command, settings=settings)
    verification.validate_receipt(root, folder, "M1", receipt, settings=None)
    with pytest.raises(verification.VerificationError, match="정책"):
        verification.validate_receipt(root, folder, "M1", receipt, settings={})
    _, ordinary, _ = verification.run(root, folder, "M1", plan, allow_command, settings=None)
    assert ordinary["status"] == "pass" and "verificationPolicy" not in ordinary
    verification.validate_receipt(root, folder, "M1", ordinary, settings=None)


@pytest.fixture
def mission(tmp_path, monkeypatch):
    root = tmp_path / "project"
    adapter = root / ".claude/orchestrator"
    adapter.mkdir(parents=True)
    (adapter / "adapter.json").write_text(json.dumps({
        "schema": 1, "paths": {"stateDir": "state"}, "verify": {"grades": ["pytest-pass", "editmode-pass"]},
        "guard": {"deny": [{"pattern": "\\bsubmit\\b", "reason": "submit 금지"}]},
    }), encoding="utf-8")
    state = root / "state"
    state.mkdir()
    (state / "ACTIVE").write_text('{"id":"m1","session":"s1"}', encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "none"))
    assert mission_state.main(["init"]) == 0
    assert mission_state.main([
        "unit", "add", "M1", "--goal", "계약", "--files", "src.py", "--done", "동작 보존",
        "--verify", "verify", "--timebox", "60", "--forbid", "submit",
    ]) == 0
    assert mission_state.main(["unit", "set", "M1", "--status", "doing"]) == 0
    (root / "src.py").write_text("value = 1\n", encoding="utf-8")
    script = root / "check.py"
    script.write_text("from pathlib import Path\np=Path('runs')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n", encoding="utf-8")
    plan = {
        "grade": "pytest-pass", "inputs": ["src.py", "check.py"], "environment": {"target": "test"},
        "commands": [{"argv": [sys.executable, str(script)], "timeoutSeconds": 10}],
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return root, state / "m1", plan


def verify(mission):
    root, folder, _ = mission
    code = mission_state.main(["verify", "M1", "--plan", str(root / "plan.json")])
    path = Path(json.loads((folder / "verification/M1.latest.json").read_text())["receipt"])
    return code, path


def review(mission, receipt, status="clean"):
    root, folder, _ = mission
    evidence = root / "review.md"
    evidence.write_text("독립 리뷰 결과\n", encoding="utf-8")
    assert mission_state.main([
        "review", "M1", "--receipt", str(receipt), "--evidence", str(evidence),
        "--reviewer", "fresh-reviewer", "--kind", "unit", "--status", status,
    ]) == 0
    return Path(json.loads((folder / "verification/M1.review.latest.json").read_text())["review"])


def test_review_counts_match_status_and_preserve_unknown_legacy_values(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    previous = review(mission, receipt)
    assert json.loads(previous.read_text())['findingsCount'] == -1
    args = ['review', 'M1', '--receipt', str(receipt), '--evidence', str(root / 'review.md'),
            '--reviewer', 'fresh-reviewer', '--kind', 'unit', '--status', 'clean',
            '--findings-count', '0', '--fixed-count', '2']
    assert mission_state.main(args) == 0
    path = Path(json.loads((folder / 'verification/M1.review.latest.json').read_text())['review'])
    record = json.loads(path.read_text())
    assert record['findingsCount'] == 0 and record['fixedCount'] == 2 and record['recordedAt']
    args[args.index('--findings-count') + 1] = '1'
    assert mission_state.main(args) == 1
    assert json.loads((folder / 'verification/M1.review.latest.json').read_text())['review'] == str(path)


def test_unchanged_verification_is_reused_and_source_or_environment_change_runs_again(mission):
    root, _, plan = mission
    first_code, first = verify(mission)
    assert first_code == 0 and (root / "runs").read_text() == "x"
    assert verify(mission) == (0, first)
    assert (root / "runs").read_text() == "x"
    (root / "src.py").write_text("value = 2\n")
    assert verify(mission)[1] != first
    assert (root / "runs").read_text() == "xx"
    plan["environment"]["target"] = "other"
    (root / "plan.json").write_text(json.dumps(plan))
    verify(mission)
    assert (root / "runs").read_text() == "xxx"


def test_complete_requires_clean_review_and_current_verification_and_is_idempotent(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    findings = review(mission, receipt, "findings")
    command = ["complete", "M1", "--receipt", str(receipt), "--review", str(findings), "--done-evidence", "動作確認"]
    before = (folder / "ledger.tsv").read_bytes()
    assert mission_state.main(command) == 1
    assert (folder / "ledger.tsv").read_bytes() == before
    clean = review(mission, receipt)
    command[command.index(str(findings))] = str(clean)
    assert mission_state.main(command) == 0
    lines = (folder / "ledger.tsv").read_text().splitlines()
    assert len(lines) == 2 and "\tpass\tpytest-pass\t" in lines[-1]
    assert (folder / "units.tsv").read_text().rstrip().endswith("\tdone")
    assert mission_state.main(command) == 0
    assert (folder / "ledger.tsv").read_text().splitlines() == lines
    (root / "src.py").write_text("value = 9\n")
    assert mission_state.main(command) == 1


def test_verification_command_gets_closed_stdin(mission, monkeypatch):
    seen = []
    real_run = verification.subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs.get("stdin"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(verification.subprocess, "run", spy)
    code, _ = verify(mission)
    assert code == 0
    assert seen == [verification.subprocess.DEVNULL]


def test_failure_is_not_cached_and_guard_rejection_never_runs_command(mission):
    root, _, plan = mission
    plan["commands"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    (root / "plan.json").write_text(json.dumps(plan))
    first_code, first = verify(mission)
    assert first_code == 1 and json.loads(first.read_text())["exitCodes"] == [7]
    assert verify(mission)[1] != first
    plan["commands"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('runs').write_text('BAD')", "submit"]
    (root / "plan.json").write_text(json.dumps(plan))
    assert mission_state.main(["verify", "M1", "--plan", str(root / "plan.json")]) == 1
    assert not (root / "runs").exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("latest_status", ["findings", "clean"])
def test_complete_rejects_superseded_clean_review(mission, runtime, latest_status):
    _, folder, _ = mission
    _, receipt = verify(mission)
    old_clean = review(mission, receipt)
    latest = review(mission, receipt, latest_status)
    command = [
        "complete", "M1", "--receipt", str(receipt), "--review", str(old_clean),
        "--done-evidence", "checked",
    ]
    before = {name: (folder / name).read_bytes() for name in ("units.tsv", "ledger.tsv")}
    assert mission_state.main(command, runtime=runtime) == 1
    assert all((folder / name).read_bytes() == data for name, data in before.items())

    command[command.index("--review") + 1] = str(latest)
    if latest_status == "findings":
        assert mission_state.main(command, runtime=runtime) == 1
        assert all((folder / name).read_bytes() == data for name, data in before.items())
        command[command.index("--review") + 1] = str(review(mission, receipt))
    assert mission_state.main(command, runtime=runtime) == 0
    assert (folder / "units.tsv").read_text().rstrip().endswith("\tdone")


def test_complete_requires_latest_review_pointer(mission):
    _, folder, _ = mission
    _, receipt = verify(mission)
    clean = review(mission, receipt)
    (folder / "verification/M1.review.latest.json").unlink()
    before = {name: (folder / name).read_bytes() for name in ("units.tsv", "ledger.tsv")}
    assert mission_state.main([
        "complete", "M1", "--receipt", str(receipt), "--review", str(clean),
        "--done-evidence", "checked",
    ]) == 1
    assert all((folder / name).read_bytes() == data for name, data in before.items())


def test_editmode_rejects_zero_tests_wrong_project_and_stale_result(mission):
    root, _, plan = mission
    plan["grade"] = "editmode-pass"
    plan["results"] = [{
        "path": "result.json", "equals": {"status": "passed"}, "positive": ["tests.0.startedCount"],
        "projectField": "projectPath",
    }]
    for project, count, expected in [(str(root), 0, 1), (str(root.parent), 2, 1), (str(root), 2, 0)]:
        payload = {"status": "passed", "projectPath": project, "tests": [{"startedCount": count}]}
        plan["commands"][0]["argv"] = [sys.executable, "-c", f"from pathlib import Path; Path('result.json').write_text({json.dumps(payload)!r})"]
        (root / "plan.json").write_text(json.dumps(plan))
        assert verify(mission)[0] == expected
    plan["commands"][0]["argv"] = [sys.executable, "-c", "pass"]
    (root / "plan.json").write_text(json.dumps(plan))
    assert verify(mission)[0] == 1


def test_changed_during_run_and_tampered_results_are_rejected(mission):
    root, folder, plan = mission
    _, receipt = verify(mission)
    data = json.loads(receipt.read_text())
    Path(data["log"]).write_text("changed")
    with pytest.raises(verification.VerificationError, match="로그"):
        verification.validate_receipt(str(root), str(folder), "M1", data)
    plan["commands"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('src.py').write_text('changed')"]
    (root / "plan.json").write_text(json.dumps(plan))
    assert verify(mission)[0] == 1


def test_review_scope_reports_additions_and_deletions(mission):
    root, folder, plan = mission
    source = root / "src"
    source.mkdir()
    (source / "a.py").write_text("a")
    plan["inputs"] = ["src"]
    (root / "plan.json").write_text(json.dumps(plan))
    _, receipt = verify(mission)
    review(mission, receipt)
    plan["unitBrief"] = json.loads(receipt.read_text())["plan"]["unitBrief"]
    assert verification.review_scope(str(root), str(folder), "M1", plan) == []
    (source / "b.py").write_text("b")
    (source / "a.py").unlink()
    assert verification.review_scope(str(root), str(folder), "M1", plan) == ["src/a.py", "src/b.py"]


def test_context_is_bounded_and_keeps_history_outside_prompt(mission, capsys):
    _, folder, _ = mission
    for index in range(20):
        assert mission_state.main([
            "decision", "add", "--what", f"decision {index}", "--chosen", "x" * 500,
            "--why", "reason", "--affects", "M1",
        ]) == 0
    capsys.readouterr()
    assert mission_state.main(["context"]) == 0
    output = capsys.readouterr().out
    assert len(output) < 2200 and "D20" in output and "D1:" not in output
    assert str(folder / "mission.md") in output
    assert (folder / "decisions.tsv").stat().st_size > 10000


def test_codex_entrypoint_uses_same_receipts(mission):
    root, _, _ = mission
    assert mission_state.main(["verify", "M1", "--plan", str(root / "plan.json")], runtime="codex") == 0
    assert (root / "runs").read_text() == "x"
    assert verify(mission)[0] == 0 and (root / "runs").read_text() == "x"


def test_inputs_outside_project_are_rejected(mission):
    root, _, plan = mission
    plan["inputs"] = ["../secret"]
    (root / "plan.json").write_text(json.dumps(plan))
    assert mission_state.main(["verify", "M1", "--plan", str(root / "plan.json")]) == 1
    assert not (root / "runs").exists()


def test_complete_rejects_changed_contract_and_review_evidence(mission):
    root, _, _ = mission
    _, receipt = verify(mission)
    checked = review(mission, receipt)
    command = ["complete", "M1", "--receipt", str(receipt), "--review", str(checked), "--done-evidence", "checked"]
    (root / "review.md").write_text("tampered")
    assert mission_state.main(command) == 1
    checked = review(mission, receipt)
    command[command.index("--review") + 1] = str(checked)
    assert mission_state.main(["unit", "set", "M1", "--done", "새 완료 조건"]) == 0
    assert mission_state.main(command) == 1


def test_later_failed_run_cannot_be_hidden_by_an_older_pass(mission):
    root, _, plan = mission
    _, receipt = verify(mission)
    checked = review(mission, receipt)
    plan["commands"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    (root / "plan.json").write_text(json.dumps(plan))
    assert verify(mission)[0] == 1
    assert mission_state.main([
        "complete", "M1", "--receipt", str(receipt), "--review", str(checked), "--done-evidence", "old pass",
    ]) == 1


def reusable_plan(mission):
    root, _, plan = mission
    script = root / "build.py"
    script.write_text(
        "from pathlib import Path\n"
        "counter=Path('build_runs')\n"
        "counter.write_text(counter.read_text()+'x' if counter.exists() else 'x')\n"
        "Path('artifact').mkdir(exist_ok=True)\n"
        "Path('artifact/result.json').write_text('{\"status\":\"passed\"}')\n",
        encoding="utf-8",
    )
    plan.update(inputs=["src.py"], commands=[
        {"id": "build", "argv": [sys.executable, "build.py"], "reusable": True, "outputs": ["artifact"]},
        {"id": "check", "argv": [sys.executable, "-c", "raise SystemExit(7)"]},
    ], results=[{"path": "artifact/result.json", "command": "build", "equals": {"status": "passed"}}])
    save_plan(mission)
    return plan


def save_plan(mission):
    root, _, plan = mission
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def receipt_data(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_later_argv_fix_reuses_only_valid_prefix_and_original_result(mission):
    root, folder, _ = mission
    plan = reusable_plan(mission)
    code, first = verify(mission)
    assert code == 1
    second = verify(mission)[1]
    record = receipt_data(second)
    assert [step["status"] for step in record["steps"]] == ["reused", "failed"]
    assert record["steps"][0]["sourceReceipt"] == str(first)
    assert record["failure"]["repeatCount"] == 2
    assert record["priorReceipt"] == str(first)
    plan["commands"][1]["argv"] = [sys.executable, "-c", "pass"]
    save_plan(mission)
    code, final = verify(mission)
    record = receipt_data(final)
    assert code == 0 and (root / "build_runs").read_text() == "x"
    assert record["executionCounts"] == {"executed": 1, "reused": 1}
    assert record["steps"][0]["sourceReceipt"] == str(first)
    verification.validate_receipt(root, folder, "M1", record)
    assert verify(mission) == (0, final)


@pytest.mark.parametrize("change", ["output-add", "output-delete", "output-edit", "input", "environment",
                                         "script", "brief", "original-log", "original-receipt"])
def test_reuse_invalidates_changed_evidence_and_dependencies(mission, monkeypatch, change):
    root, _, _ = mission
    plan = reusable_plan(mission)
    _, first = verify(mission)
    if change == "output-add":
        (root / "artifact/extra").write_text("extra")
    elif change == "output-delete":
        (root / "artifact/result.json").unlink()
    elif change == "output-edit":
        (root / "artifact/result.json").write_text('{"status":"failed"}')
    elif change == "input":
        (root / "src.py").write_text("changed")
    elif change == "environment":
        plan["environment"]["target"] = "changed"
    elif change == "script":
        script = root / "build.py"
        script.write_text(script.read_text(encoding="utf-8") + "\nvalue=2\n", encoding="utf-8")
    elif change == "brief":
        assert mission_state.main(["unit", "set", "M1", "--done", "new completion condition"]) == 0
    elif change == "original-log":
        Path(receipt_data(first)["log"]).write_text("tampered")
    else:
        verify(mission)
        first.write_text(first.read_text(encoding="utf-8") + " ", encoding="utf-8")
    plan["commands"][1]["argv"] = [sys.executable, "-c", "pass"]
    save_plan(mission)
    assert verify(mission)[0] == 0
    assert (root / "build_runs").read_text() == "xx"


def runtime_key_in_new_process(mission, cwd, extra):
    root, _, _ = mission
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import verification; "
            "print(verification.runtime_key(sys.argv[2], json.load(open(sys.argv[3], encoding='utf-8'))))")
    done = subprocess.run(
        [sys.executable, "-c", code, str(Path(verification.__file__).parent), str(root), str(root / "plan.json")],
        cwd=cwd, env={**os.environ, **extra}, capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def test_runtime_key_ignores_session_and_shell_environment_across_processes(mission, tmp_path):
    root, _, _ = mission
    first = runtime_key_in_new_process(mission, root, {"PWD": str(root), "CLAUDE_SESSION_ID": "session-a"})
    second = runtime_key_in_new_process(mission, tmp_path, {
        "PWD": str(tmp_path), "CLAUDE_SESSION_ID": "session-b", "CLAUDE_AGENT_ID": "agent-b",
        "SHLVL": "7", "FICTIONAL_CHECK_MODE": "changed",
    })
    assert first == second


def test_undeclared_environment_change_still_reuses_receipt_and_steps(mission, monkeypatch):
    root, _, _ = mission
    _, first = verify(mission)
    monkeypatch.setenv("PWD", str(root / "elsewhere"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "other-session")
    monkeypatch.setenv("FICTIONAL_CHECK_MODE", "changed")
    assert verify(mission) == (0, first)
    assert (root / "runs").read_text() == "x"
    clean = review(mission, first)
    assert mission_state.main(["complete", "M1", "--receipt", str(first), "--review", str(clean),
                               "--done-evidence", "checked"]) == 0

    plan = reusable_plan(mission)
    plan["commands"][1]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    save_plan(mission)
    verify(mission)
    monkeypatch.setenv("FICTIONAL_CHECK_MODE", "changed-again")
    record = receipt_data(verify(mission)[1])
    assert [step["status"] for step in record["steps"]] == ["reused", "failed"]
    assert (root / "build_runs").read_text() == "x"


def test_declared_environment_value_is_part_of_runtime_identity(mission, monkeypatch):
    root, _, plan = mission
    plan["environmentKeys"] = ["FICTIONAL_CHECK_MODE"]
    save_plan(mission)
    monkeypatch.delenv("FICTIONAL_CHECK_MODE", raising=False)
    _, first = verify(mission)
    assert verify(mission) == (0, first)
    monkeypatch.setenv("FICTIONAL_CHECK_MODE", "strict")
    with pytest.raises(verification.VerificationError, match="런타임"):
        verification.validate_receipt(root, mission[1], "M1", receipt_data(first))
    second = verify(mission)[1]
    assert second != first and (root / "runs").read_text() == "xx"
    assert verify(mission) == (0, second)
    monkeypatch.setenv("FICTIONAL_CHECK_MODE", "lenient")
    assert verify(mission)[1] != second and (root / "runs").read_text() == "xxx"


def test_declared_environment_value_invalidates_reusable_step(mission, monkeypatch):
    root, _, _ = mission
    plan = reusable_plan(mission)
    plan["environmentKeys"] = ["FICTIONAL_CHECK_MODE"]
    save_plan(mission)
    monkeypatch.setenv("FICTIONAL_CHECK_MODE", "strict")
    verify(mission)
    monkeypatch.setenv("FICTIONAL_CHECK_MODE", "lenient")
    verify(mission)
    assert (root / "build_runs").read_text() == "xx"


@pytest.mark.parametrize("value", ["PATH", ["A", "A"], [""], ["A=B"], [1], ["A\nB"]])
def test_environment_keys_must_be_unique_variable_names(mission, value):
    root, _, plan = mission
    plan["environmentKeys"] = value
    save_plan(mission)
    assert mission_state.main(["verify", "M1", "--plan", str(root / "plan.json")]) == 1
    assert not (root / "runs").exists()


def test_receipt_with_old_runtime_fingerprint_is_not_trusted_and_reruns(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    data = receipt_data(receipt)
    data.pop("runnerVersion")
    receipt.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="다시 실행"):
        verification.validate_receipt(root, folder, "M1", data)
    evidence = root / "review.md"
    evidence.write_text("independent review\n", encoding="utf-8")
    assert mission_state.main([
        "review", "M1", "--receipt", str(receipt), "--evidence", str(evidence),
        "--reviewer", "fresh-reviewer", "--kind", "unit", "--status", "clean",
    ]) == 1
    second = verify(mission)[1]
    assert second != receipt and (root / "runs").read_text() == "xx"
    assert receipt_data(second)["runnerVersion"] == verification.RUNNER_VERSION


def test_force_and_default_nonreusable_steps_execute_again(mission):
    root, _, _ = mission
    plan = reusable_plan(mission)
    plan["commands"][0].pop("reusable")
    save_plan(mission)
    verify(mission)
    verify(mission)
    assert (root / "build_runs").read_text() == "xx"
    plan["commands"][0]["reusable"] = True
    plan["commands"][1]["argv"] = [sys.executable, "-c", "pass"]
    save_plan(mission)
    assert verify(mission)[0] == 0
    assert mission_state.main(["verify", "M1", "--plan", str(root / "plan.json"), "--force"]) == 0
    assert (root / "build_runs").read_text() == "xxxx"


def test_reexecuted_earlier_step_forces_later_reusable_step_to_execute(mission):
    root, _, plan = mission
    plan["commands"] = [
        {"argv": [sys.executable, "check.py"]},
        {"argv": [sys.executable, "check.py"], "reusable": True, "outputs": []},
        {"argv": [sys.executable, "-c", "raise SystemExit(7)"]},
    ]
    save_plan(mission)
    verify(mission)
    verify(mission)
    assert (root / "runs").read_text() == "xxxx"


def test_script_after_interpreter_flags_is_part_of_reuse_identity(mission):
    root, _, _ = mission
    plan = reusable_plan(mission)
    plan["commands"][0]["argv"] = [sys.executable, "-B", "build.py"]
    save_plan(mission)
    verify(mission)
    script = root / "build.py"
    script.write_text(script.read_text(encoding="utf-8") + "\nvalue=3\n", encoding="utf-8")
    verify(mission)
    assert (root / "build_runs").read_text() == "xx"


@pytest.mark.parametrize("later_exit", [False, True])
def test_failed_run_with_mutated_input_cannot_reuse_steps_after_source_restoration(mission, later_exit):
    root, folder, plan = mission
    (root / "src.py").write_text("original", encoding="utf-8")
    script = root / "mutating.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('src.py').write_text('changed')\n"
        "Path('artifact').write_text(Path('src.py').read_text())\n", encoding="utf-8",
    )
    plan.update(inputs=["src.py", "mutating.py"], commands=[
        {"argv": [sys.executable, "mutating.py"], "reusable": True, "outputs": ["artifact"]},
    ])
    if later_exit:
        plan["commands"].append({"argv": [sys.executable, "-c", "raise SystemExit(7)"]})
    save_plan(mission)
    code, first = verify(mission)
    assert code == 1 and receipt_data(first)["cacheIntegrity"]["status"] == "invalid"
    assert receipt_data(first)["failure"]["category"] == "input_changed"
    (root / "src.py").write_text("original", encoding="utf-8")
    if later_exit:
        plan["commands"][1]["argv"] = [sys.executable, "-c", "pass"]
    save_plan(mission)
    code, second = verify(mission)
    record = receipt_data(second)
    assert code == 1 and record["executionCounts"]["reused"] == 0
    assert record["failure"]["category"] == "input_changed"
    assert (root / "artifact").read_text() == "changed"
    with pytest.raises(verification.VerificationError):
        verification.validate_receipt(root, folder, "M1", record)


def test_later_failing_command_mutation_invalidates_earlier_valid_prefix(mission):
    root, _, _ = mission
    plan = reusable_plan(mission)
    plan["commands"][1]["argv"] = [sys.executable, "-c",
        "from pathlib import Path; Path('src.py').write_text('changed'); raise SystemExit(7)"]
    save_plan(mission)
    code, first = verify(mission)
    record = receipt_data(first)
    assert code == 1 and record["failure"]["category"] == "input_changed"
    assert record["steps"][1]["exitCode"] == 7 and record["cacheIntegrity"]["status"] == "invalid"
    (root / "src.py").write_text("value = 1\n", encoding="utf-8")
    plan["commands"][1]["argv"] = [sys.executable, "-c", "pass"]
    save_plan(mission)
    assert verify(mission)[0] == 0
    assert (root / "build_runs").read_text() == "xx"


def test_failed_receipt_without_global_integrity_proof_cannot_supply_prefix(mission):
    root, _, _ = mission
    reusable_plan(mission)
    _, first = verify(mission)
    record = receipt_data(first)
    record.pop("cacheIntegrity")
    verification.write_json(first, record)
    verify(mission)
    assert (root / "build_runs").read_text() == "xx"


@pytest.mark.parametrize("flags", [[], ["-B"], ["-X", "utf8", "-W", "ignore"]])
@pytest.mark.parametrize("declared_result", [False, True])
def test_legacy_script_output_arguments_do_not_change_runtime_identity(mission, flags, declared_result):
    root, _, plan = mission
    (root / "writer.py").write_text(
        "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('{\"status\":\"passed\"}')\n",
        encoding="utf-8",
    )
    plan["inputs"] = ["src.py", "writer.py"]
    plan["commands"] = [{"argv": [sys.executable, *flags, "writer.py", "output.json"]}]
    if declared_result:
        plan["results"] = [{"path": "output.json", "equals": {"status": "passed"}}]
    save_plan(mission)
    assert verify(mission)[0] == 0
    assert mission_state.main(["verify", "M1", "--plan", str(root / "plan.json"), "--force"]) == 0


@pytest.mark.parametrize("arguments", [["-c", "program", "output.py"], ["-m", "module", "output.py"],
                                        ["-cprogram", "output.py"], ["-mmodule", "output.py"], ["-", "output.py"]])
def test_python_inline_and_module_arguments_are_not_scripts(mission, arguments):
    root, _, _ = mission
    (root / "output.py").write_text("first", encoding="utf-8")
    command = {"argv": [sys.executable, *arguments]}
    first = verification.command_runtime(root, command)
    (root / "output.py").write_text("changed", encoding="utf-8")
    assert verification.command_runtime(root, command) == first


@pytest.mark.parametrize("change", ["missing-outputs", "missing-producer", "wrong-producer", "unowned-result"])
def test_reusable_result_requires_explicit_producer_and_owned_outputs(mission, change):
    root, _, _ = mission
    plan = reusable_plan(mission)
    if change == "missing-outputs":
        plan["commands"][0].pop("outputs")
    elif change == "missing-producer":
        plan["results"][0].pop("command")
    elif change == "wrong-producer":
        plan["results"][0]["command"] = "absent"
    else:
        plan["results"][0]["command"] = 1
    save_plan(mission)
    code, path = verify(mission)
    assert code == 1 and receipt_data(path)["failure"]["category"] == "plan_invalid"
    assert not (root / "build_runs").exists()


def test_result_freshness_is_checked_at_its_producer_not_later_command(mission):
    root, _, plan = mission
    (root / "result.json").write_text('{"status":"passed"}')
    plan["commands"] = [
        {"id": "producer", "argv": [sys.executable, "-c", "pass"], "outputs": ["result.json"]},
        {"argv": [sys.executable, "-c", "from pathlib import Path; Path('result.json').write_text('{}')"]},
    ]
    plan["results"] = [{"path": "result.json", "command": "producer", "equals": {"status": "passed"}}]
    save_plan(mission)
    code, path = verify(mission)
    record = receipt_data(path)
    assert code == 1 and record["failure"]["category"] == "result_invalid"
    assert record["steps"][1]["status"] == "not_run"
    assert json.loads((root / "result.json").read_text())["status"] == "passed"


@pytest.mark.parametrize("change,category", [("control", "preflight"), ("cwd-missing", "preflight"),
                                                ("cwd-outside", "preflight"), ("executable", "preflight"),
                                                ("plan", "plan_invalid"), ("guard", "guard_rejected")])
def test_prelaunch_failures_supersede_old_pass_and_never_execute(mission, change, category):
    root, folder, plan = mission
    _, old = verify(mission)
    checked = review(mission, old)
    if change == "control":
        plan["commands"][0]["argv"].append("bad\nargument")
    elif change == "cwd-missing":
        plan["commands"][0]["cwd"] = "missing"
    elif change == "cwd-outside":
        plan["commands"][0]["cwd"] = ".."
    elif change == "executable":
        plan["commands"][0]["argv"][0] = "fictional-missing-check-program"
    elif change == "plan":
        plan["commands"] = []
    else:
        plan["commands"][0]["argv"].append("submit")
    save_plan(mission)
    code, path = verify(mission)
    record = receipt_data(path)
    assert code == 1 and path != old and record["failure"]["category"] == category
    assert record["startedAt"] and record["finishedAt"] and record["recordedAt"]
    assert (root / "runs").read_text() == "x"
    assert mission_state.main(["complete", "M1", "--receipt", str(old), "--review", str(checked),
                               "--done-evidence", "old proof"]) == 1
    assert receipt_data(folder / "verification/M1.latest.json")["receipt"] == str(path)


def test_resolved_executable_is_executed_relative_to_command_cwd(mission, monkeypatch):
    root, _, plan = mission
    work = root / "work"
    work.mkdir()
    local = work / Path(sys.executable).name
    local.write_bytes(b"fictional executable identity")
    local.chmod(0o755)
    plan["commands"] = [{"cwd": "work", "argv": [local.name, "-c", "pass"]}]
    seen = []

    def launch(argv, **kwargs):
        seen.append((argv, kwargs))
        return verification.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(verification.subprocess, "run", launch)
    save_plan(mission)
    assert verify(mission)[0] == 0
    assert seen[0][0][0] == str(local.resolve())
    assert seen[0][1]["cwd"] == work.resolve() and seen[0][1]["shell"] is False


@pytest.mark.parametrize("failure,category", [("timeout", "timeout"), ("launch", "launch_error"),
                                               ("input", "input_changed"), ("runtime", "runtime_changed")])
def test_typed_execution_failures(mission, monkeypatch, failure, category):
    root, _, plan = mission
    if failure in ("timeout", "launch"):
        def launch(argv, **kwargs):
            if failure == "timeout":
                raise verification.subprocess.TimeoutExpired(argv, 1)
            raise OSError("fictional launch failure")
        monkeypatch.setattr(verification.subprocess, "run", launch)
    elif failure == "input":
        plan["commands"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('src.py').write_text('changed')"]
    else:
        plan["environmentKeys"] = ["FICTIONAL_RUNTIME_MODE"]
        real = verification.subprocess.run
        def launch(*args, **kwargs):
            result = real(*args, **kwargs)
            monkeypatch.setenv("FICTIONAL_RUNTIME_MODE", "changed")
            return result
        monkeypatch.setattr(verification.subprocess, "run", launch)
    save_plan(mission)
    code, path = verify(mission)
    assert code == 1 and receipt_data(path)["failure"]["category"] == category


def structured_review(mission, receipt, number, findings, resolved=(), status="findings", **counts):
    root, folder, _ = mission
    source = root / f"findings-{number}.json"
    source.write_text(json.dumps({"schema": 1, "findings": findings, "resolvedIds": list(resolved),
                                  "suggestions": [{"text": "optional fictional improvement"}]}), encoding="utf-8")
    evidence = root / f"review-{number}.md"
    evidence.write_text(f"Fictional independent review {number}\n", encoding="utf-8")
    path = verification.record_review(root, folder, "M1", receipt, evidence, "reviewer", "unit", status,
                                      findings_input=source, **counts)
    return Path(path)


def finding(identity="F1", kind="defect", origin="new", **fields):
    return {"id": identity, "kind": kind, "origin": origin, "reason": "Fictional source contract mismatch", **fields}


def test_structured_reviews_preserve_identity_resolution_regression_and_counts(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    first = structured_review(mission, receipt, 1, [finding(), finding("F2", "rule_violation")])
    second = structured_review(mission, receipt, 2, [finding(origin="unresolved", relatedId="F1")], ["F2"])
    clean = structured_review(mission, receipt, 3, [], ["F1"], "clean")
    record = receipt_data(clean)
    assert record["findingsCount"] == 0 and record["fixedCount"] == 1 and record["round"] == 3
    assert record["previousReview"] == str(second) and record["previousReviewHash"] == verification.file_hash(second)
    assert record["diagnostics"] and receipt_data(first)["newFindingsCount"] == 2
    verification.validate_review(folder, "M1", receipt, clean)
    regression = structured_review(mission, receipt, 4, [finding("F3", origin="regression", relatedId="F1")])
    assert receipt_data(regression)["newFindingsCount"] == 0 and receipt_data(regression)["diagnostics"]
    with pytest.raises(verification.VerificationError, match="마지막 리뷰"):
        verification.validate_review(folder, "M1", receipt, clean)


@pytest.mark.parametrize("invalid", ["new-existing", "unresolved-new", "regression-open", "regression-no-link",
                                       "duplicate", "kind", "missing-reason", "count", "drop", "legacy-drop"])
def test_structured_reviews_reject_unfounded_identity_and_clean_claims(mission, invalid):
    root, folder, _ = mission
    _, receipt = verify(mission)
    previous = structured_review(mission, receipt, 1, [finding()])
    data = [finding(origin="unresolved", relatedId="F1")]
    status, resolved, counts = "findings", [], {}
    if invalid == "new-existing":
        data = [finding()]
    elif invalid == "unresolved-new":
        data.append(finding("F2", origin="unresolved"))
    elif invalid == "regression-open":
        data.append(finding("F2", origin="regression", relatedId="F1"))
    elif invalid == "regression-no-link":
        data.append(finding("F2", origin="regression"))
    elif invalid == "duplicate":
        data *= 2
    elif invalid == "kind":
        data[0]["kind"] = "suggestion"
    elif invalid == "missing-reason":
        data.append(finding("F2", reason=""))
    elif invalid == "count":
        counts["findings_count"] = 2
    elif invalid == "drop":
        data, status = [], "clean"
    else:
        with pytest.raises(verification.VerificationError, match="findings-input"):
            verification.record_review(root, folder, "M1", receipt, root / "review-1.md", "reviewer", "unit", "clean")
        return
    with pytest.raises(verification.VerificationError):
        structured_review(mission, receipt, 2, data, resolved, status, **counts)
    assert receipt_data(folder / "verification/M1.review.latest.json")["review"] == str(previous)


def test_review_cli_accepts_structured_findings_and_rejects_changed_source(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    source = root / "findings.json"
    source.write_text('{"findings":[],"resolvedIds":[]}', encoding="utf-8")
    report = root / "report.md"
    report.write_text("Fictional review evidence", encoding="utf-8")
    assert mission_state.main(["review", "M1", "--receipt", str(receipt), "--evidence", str(report),
                               "--reviewer", "reviewer", "--kind", "unit", "--status", "clean",
                               "--findings-input", str(source)]) == 0
    path = Path(receipt_data(folder / "verification/M1.review.latest.json")["review"])
    source.write_text('{"findings":[{}]}', encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="원본"):
        verification.validate_review(folder, "M1", receipt, path)
