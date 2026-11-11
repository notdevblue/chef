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


@pytest.fixture
def inspect_plan():
    return {"grade": "pytest-pass", "inputs": ["src", "tests"], "environment": {"target": "fictional-secret"},
            "referenceBundle": "references.json", "commands": [
                {"id": "prepare", "argv": ["fictional-tool", "secret-argument"], "reusable": True, "outputs": []},
                {"id": "check", "argv": ["fictional-check"]},
                {"id": "finish", "argv": ["fictional-finish"]},
            ]}


@pytest.fixture
def inspect_receipt(inspect_plan):
    return {"schema": 1, "runnerVersion": 2, "unit": "M1", "grade": "pytest-pass", "status": "fail",
            "plan": inspect_plan, "snapshot": {"src/main.py": "input-hash"}, "results": {},
            "log": "never-open.log", "logHash": "log-hash", "error": "fictional command failure",
            "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:00:01Z", "seconds": 1.0,
            "executionCounts": {"executed": 1, "reused": 1},
            "failure": {"category": "command_exit", "step": 1, "signature": "failure-signature",
                        "repeatCount": 3, "previousReceipt": "never-open-previous.json"},
            "priorReceipt": "never-open-previous.json", "priorReceiptHash": "previous-hash",
            "steps": [{"index": 0, "id": "prepare", "status": "reused", "sourceReceipt": "never-open-source.json",
                       "sourceReceiptHash": "source-hash", "sourceStep": 0},
                      {"index": 1, "id": "check", "status": "failed", "exitCode": 2},
                      {"index": 2, "id": "finish", "status": "not_run"}]}


def test_inspect_plan_and_receipt_only_read_source(tmp_path, inspect_plan, inspect_receipt, monkeypatch):
    source = tmp_path / "artifact.json"
    real_read = Path.read_bytes
    opened = []

    def read(path):
        opened.append(path)
        assert path == source
        return real_read(path)

    def unexpected(*args, **kwargs):
        pytest.fail("inspect performed an execution or validity check")

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(verification.subprocess, "run", unexpected)
    monkeypatch.setattr(verification, "validate_plan", unexpected)
    monkeypatch.setattr(verification, "validate_receipt", unexpected)
    monkeypatch.setattr(verification, "runtime_key", unexpected)
    for kind, value in (("plan", inspect_plan), ("receipt", inspect_receipt)):
        source.write_text(json.dumps(value), encoding="utf-8")
        before = real_read(source)
        result = verification.inspect_artifact(source, kind)
        assert result["source"] == {"path": str(source.resolve()), "sha256": verification.hashlib.sha256(before).hexdigest()}
        assert result["current_validity"] == "not_checked"
        assert result["counts"]["inputEntries"] == 2
        assert result["counts"]["referenceBundles"] == 1
        assert result["counts"]["commands"] == 3
        assert result["steps"]["omitted"] == 0
        assert "fictional-secret" not in json.dumps(result)
        assert "secret-argument" not in json.dumps(result)
        assert "input-hash" not in json.dumps(result)
        assert real_read(source) == before
    assert opened == [source, source]
    assert list(tmp_path.iterdir()) == [source]
    assert result["statusCounts"] == {"passed": 0, "failed": 1, "reused": 1, "not_run": 1}
    assert result["executionCounts"] == {"executed": 1, "reused": 1}
    assert result["failure"]["repeatCount"] == 3
    assert result["failedSteps"]["items"] == [{"index": 1, "id": "check", "exitCode": 2}]
    assert result["steps"]["items"][0]["sourceReceipt"] == "never-open-source.json"
    assert result["recorded"]["priorReceipt"] == "never-open-previous.json"


def test_inspect_recorded_pass_does_not_check_current_inputs(tmp_path, inspect_receipt):
    inspect_receipt["status"] = "pass"
    inspect_receipt["steps"][1]["status"] = "passed"
    inspect_receipt["steps"][2]["status"] = "passed"
    inspect_receipt["executionCounts"]["executed"] = 2
    inspect_receipt["error"] = ""
    inspect_receipt["failure"] = {"category": "", "step": -1, "repeatCount": 0}
    source = tmp_path / "pass.json"
    source.write_text(json.dumps(inspect_receipt), encoding="utf-8")
    result = verification.inspect_artifact(source, "receipt")
    assert result["identity"]["status"] == "pass"
    assert result["current_validity"] == "not_checked"
    assert result["failedSteps"] == {"items": [], "omitted": 0}
    assert not (tmp_path / "never-open.log").exists()


def test_inspect_accepts_actual_generated_receipts_and_original_plan(mission):
    root, folder, _ = mission
    plan = reusable_plan(mission)
    assert verification.inspect_artifact(root / "plan.json", "plan")["counts"]["commands"] == 2
    code, first = verify(mission)
    assert code == 1
    assert verification.inspect_artifact(first, "receipt")["statusCounts"]["failed"] == 1
    code, second = verify(mission)
    assert code == 1
    result = verification.inspect_artifact(second, "receipt")
    assert result["executionCounts"] == {"executed": 1, "reused": 1}
    assert result["failure"]["repeatCount"] == 2
    plan["commands"][1]["argv"] = [sys.executable, "-c", "pass"]
    save_plan(mission)
    code, final = verify(mission)
    assert code == 0
    assert verification.inspect_artifact(final, "receipt")["identity"]["status"] == "pass"
    path, _, _ = verification.record_failure(root, folder, "M1", "fictional invalid plan")
    result = verification.inspect_artifact(path, "receipt")
    assert result["counts"]["commands"] == 0
    assert result["failure"]["category"] == "plan_invalid"


def test_inspect_preflight_failure_and_legacy_absent_steps(tmp_path, inspect_receipt):
    source = tmp_path / "fail.json"
    inspect_receipt.update(plan={}, steps=[], executionCounts={"executed": 0, "reused": 0}, snapshot={})
    inspect_receipt["failure"].update(category="plan_invalid", step=-1)
    source.write_text(json.dumps(inspect_receipt), encoding="utf-8")
    result = verification.inspect_artifact(source, "receipt")
    assert result["counts"]["commands"] == 0
    assert result["failure"]["category"] == "plan_invalid"
    del inspect_receipt["steps"]
    del inspect_receipt["executionCounts"]
    source.write_text(json.dumps(inspect_receipt), encoding="utf-8")
    result = verification.inspect_artifact(source, "receipt")
    assert "steps" not in result and "executionCounts" not in result and "statusCounts" not in result


@pytest.mark.parametrize("kind,field,value", [
    ("plan", "kind", "receipt"), ("plan", "schema", 2), ("plan", "schema", True),
    ("plan", "commands", ["invalid"]), ("plan", "commands", []), ("plan", "inputs", "src"),
    ("plan", "environment", []), ("plan", "unit", ["invalid"]), ("plan", "grade", 1),
    ("receipt", "kind", "plan"), ("receipt", "schema", "1"), ("receipt", "status", "passed"),
    ("receipt", "snapshot", []), ("receipt", "steps", [{}]), ("receipt", "failure", []),
    ("receipt", "executionCounts", {"executed": True, "reused": 1}),
    ("receipt", "executionCounts", {"executed": 999, "reused": 1}),
    ("receipt", "seconds", float("nan")), ("receipt", "plan", []),
])
def test_inspect_rejects_wrong_artifact_structure(tmp_path, inspect_plan, inspect_receipt, kind, field, value):
    artifacts = {"plan": inspect_plan, "receipt": inspect_receipt}
    artifacts[kind][field] = value
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(artifacts[kind]), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="inspect:"):
        verification.inspect_artifact(source, kind)


def test_inspect_large_receipt_has_bounded_output_and_late_failure(tmp_path, inspect_receipt):
    size = 2000
    long_text = "x" * 10000
    inspect_receipt["plan"]["commands"] = [
        {"id": f"step-{index}-{long_text}", "argv": [long_text]} for index in range(size)]
    inspect_receipt["steps"] = [
        {"index": index, "id": f"step-{index}-{long_text}", "status": "reused", "sourceReceipt": long_text,
         "sourceReceiptHash": long_text} for index in range(size)]
    inspect_receipt["steps"][-1].update(status="failed", exitCode=1)
    inspect_receipt["executionCounts"] = {"executed": 1, "reused": size - 1}
    inspect_receipt["failure"]["step"] = size - 1
    inspect_receipt["snapshot"] = {f"src/{index}": long_text for index in range(size)}
    inspect_receipt["error"] = long_text
    source = tmp_path / "large.json"
    source.write_text(json.dumps(inspect_receipt), encoding="utf-8")
    result = verification.inspect_artifact(source, "receipt")
    assert len(json.dumps(result)) < 14000
    assert result["counts"]["snapshotFiles"] == size
    assert result["steps"]["omitted"] == size - 8
    assert len(result["steps"]["items"]) == 8
    assert result["failedSteps"]["items"][0]["index"] == size - 1
    assert result["truncatedFields"]["omitted"] > 0
    assert len(result["truncatedFields"]["items"]) == 8
    assert len(result["recorded"]["error"]) == 240
    source.write_text(json.dumps(inspect_receipt["plan"]), encoding="utf-8")
    plan_result = verification.inspect_artifact(source, "plan")
    assert len(json.dumps(plan_result)) < 4000
    assert plan_result["steps"]["omitted"] == size - 8
    assert len(plan_result["steps"]["items"]) == 8
    assert len(plan_result["truncatedFields"]["items"]) == 8


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
    assert record["diagnostics"] == [] and receipt_data(first)["newFindingsCount"] == 2
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


def low(identity="F1", kind="defect", **fields):
    return finding(identity, kind, severity="low", **fields)


def with_unit_brief(mission, receipt):
    _, _, plan = mission
    return {**plan, "unitBrief": receipt_data(receipt)["plan"]["unitBrief"]}


def start_args(mission):
    root, _, _ = mission
    return ["review-start", "M1", "--plan", str(root / "plan.json")]


def review_with_start(mission, receipt, status="clean"):
    assert mission_state.main(start_args(mission)) == 0
    return review(mission, receipt, status)


def latest_review_pointer(folder):
    return (folder / "verification/M1.review.latest.json").read_text(encoding="utf-8")


def test_finding_severity_is_optional_and_restricted(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    first = structured_review(mission, receipt, 1, [finding(severity="low"), finding("F2", "rule_violation")])
    assert [item.get("severity") for item in receipt_data(first)["findings"]] == ["low", None]
    with pytest.raises(verification.VerificationError, match="severity"):
        structured_review(mission, receipt, 2, [finding("F3", severity="minor")])
    assert receipt_data(folder / "verification/M1.review.latest.json")["review"] == str(first)


def test_closed_low_closes_low_findings_and_passes_complete(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low(), low("F2", "rule_violation")])
    closed = structured_review(mission, receipt, 2, [], ["F1", "F2"], "closed-low")
    record = receipt_data(closed)
    assert record["status"] == "closed-low" and record["findingsCount"] == 0 and record["closedLowIds"] == ["F1", "F2"]
    verification.validate_review(folder, "M1", receipt, closed)
    command = ["complete", "M1", "--receipt", str(receipt), "--review", str(closed), "--done-evidence", "checked fix diff"]
    assert mission_state.main(command) == 0
    assert (folder / "units.tsv").read_text(encoding="utf-8").rstrip().endswith("\tdone")


@pytest.mark.parametrize("case", [
    "no-history", "prior-clean", "consecutive", "prior-high", "prior-unlabeled", "prior-regression",
    "findings-left", "resolved-partial", "count", "unstructured-prior"])
def test_closed_low_rejects_when_conditions_fail(mission, case):
    root, folder, _ = mission
    _, receipt = verify(mission)
    resolved, data, counts = ["F1"], [], {}
    if case == "prior-clean":
        structured_review(mission, receipt, 1, [], [], "clean")
    elif case == "consecutive":
        structured_review(mission, receipt, 1, [low()])
        structured_review(mission, receipt, 2, [], ["F1"], "closed-low")
    elif case == "prior-high":
        structured_review(mission, receipt, 1, [finding(severity="high")])
    elif case == "prior-unlabeled":
        structured_review(mission, receipt, 1, [low(), finding("F2")])
        resolved = ["F1", "F2"]
    elif case == "prior-regression":
        structured_review(mission, receipt, 1, [low()])
        structured_review(mission, receipt, 2, [], ["F1"], "clean")
        structured_review(mission, receipt, 3, [low("F3", origin="regression", relatedId="F1")])
        resolved = ["F3"]
    elif case == "findings-left":
        structured_review(mission, receipt, 1, [low(), low("F2")])
        data = [low("F2", origin="unresolved")]
    elif case == "resolved-partial":
        structured_review(mission, receipt, 1, [low(), low("F2")])
    elif case == "count":
        structured_review(mission, receipt, 1, [low()])
        counts["findings_count"] = 1
    elif case == "unstructured-prior":
        review(mission, receipt, "findings")
    pointer = ""
    if case != "no-history":
        pointer = latest_review_pointer(folder)
    with pytest.raises(verification.VerificationError):
        structured_review(mission, receipt, 9, data, resolved, "closed-low", **counts)
    if pointer:
        assert latest_review_pointer(folder) == pointer


def test_closed_low_requires_findings_input_and_existing_evidence(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low()])
    with pytest.raises(verification.VerificationError, match="findings-input"):
        verification.record_review(root, folder, "M1", receipt, root / "review-1.md", "reviewer", "unit", "closed-low")
    source = root / "closing.json"
    source.write_text(json.dumps({"findings": [], "resolvedIds": ["F1"]}), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="리뷰 결과 파일"):
        verification.record_review(root, folder, "M1", receipt, root / "absent.md", "reviewer", "unit", "closed-low",
                                   findings_input=source)


def test_closed_low_cli_and_schema(mission, capsys):
    root, folder, _ = mission
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low()])
    source = root / "closing.json"
    source.write_text(json.dumps({"findings": [], "resolvedIds": ["F1"]}), encoding="utf-8")
    args = ["review", "M1", "--receipt", str(receipt), "--evidence", str(root / "review-1.md"), "--reviewer", "lead",
            "--kind", "unit", "--status", "closed-low"]
    assert mission_state.main(args) == 1
    assert mission_state.main([*args, "--findings-input", str(source)]) == 0
    assert mission_state.main([*args, "--findings-input", str(source)]) == 1
    capsys.readouterr()
    assert mission_state.main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["reviewStatuses"] == ["clean", "findings", "closed-low"]


def test_third_round_diagnostic_points_to_closed_low_or_question(mission):
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low()])
    structured_review(mission, receipt, 2, [low(origin="unresolved")])
    third = structured_review(mission, receipt, 3, [low(origin="unresolved")])
    text = " ".join(receipt_data(third)["diagnostics"])
    assert "closed-low" in text and "질문" in text


def test_complete_rejects_closed_low_record_with_forged_open_findings(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low()])
    closed = structured_review(mission, receipt, 2, [], ["F1"], "closed-low")
    data = receipt_data(closed)
    data["findings"] = [low()]
    closed.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="미해소 지적"):
        verification.validate_review(folder, "M1", receipt, closed)


def test_review_start_records_snapshot_copies_and_links_next_review(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    assert mission_state.main(start_args(mission)) == 0
    start_path = Path(receipt_data(folder / "verification/M1.review-start.latest.json")["reviewStart"])
    start = receipt_data(start_path)
    assert start["snapshot"] == receipt_data(receipt)["snapshot"] and start["previousReview"] == ""
    assert sorted(start["copies"]["files"]) == ["check.py", "src.py"]
    copy = Path(start["copies"]["dir"]) / start["snapshot"]["src.py"]
    assert copy.read_text(encoding="utf-8") == "value = 1\n"
    checked = receipt_data(review(mission, receipt))
    assert checked["reviewStart"] == str(start_path) and checked["reviewStartHash"] == verification.file_hash(start_path)
    assert checked["snapshot"] == start["snapshot"] and "inputChangedDuringReview" not in checked
    again = receipt_data(review(mission, receipt))
    assert "reviewStart" not in again
    assert (Path(again["copies"]["dir"]) / again["snapshot"]["src.py"]).is_file()
    assert Path(again["copies"]["dir"]) == Path(start["copies"]["dir"])


def test_review_without_start_keeps_legacy_flow_and_copies_at_review(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    path = review(mission, receipt)
    record = receipt_data(path)
    assert "reviewStart" not in record and "inputChangedDuringReview" not in record
    assert record["snapshot"] == receipt_data(receipt)["snapshot"]
    assert Path(record["copies"]["dir"]).parent == (folder / "verification").resolve()
    verification.validate_review(folder, "M1", receipt, path)


def test_input_change_during_review_is_recorded_and_blocks_complete_until_rereview(mission):
    root, folder, _ = mission
    _, first = verify(mission)
    assert mission_state.main(start_args(mission)) == 0
    (root / "src.py").write_text("value = 2\n", encoding="utf-8")
    _, second = verify(mission)
    assert second != first
    path = review(mission, second)
    record = receipt_data(path)
    assert record["inputChangedDuringReview"] == ["src.py"]
    assert any("src.py" in line for line in record["diagnostics"])
    assert record["snapshot"]["src.py"] != receipt_data(second)["snapshot"]["src.py"]
    command = ["complete", "M1", "--receipt", str(second), "--review", str(path), "--done-evidence", "checked"]
    assert mission_state.main(command) == 1
    with pytest.raises(verification.VerificationError, match="src.py"):
        verification.validate_review(folder, "M1", second, path)
    assert verification.review_scope(root, folder, "M1", with_unit_brief(mission, second)) == ["src.py"]
    rereview = review(mission, second)
    assert "inputChangedDuringReview" not in receipt_data(rereview)
    assert mission_state.main(["complete", "M1", "--receipt", str(second), "--review", str(rereview),
                               "--done-evidence", "checked"]) == 0


def test_review_start_inputs_must_match_the_verified_plan(mission):
    root, folder, plan = mission
    _, receipt = verify(mission)
    (root / "other.json").write_text(json.dumps({**plan, "inputs": ["src.py"]}), encoding="utf-8")
    (root / "review.md").write_text("Fictional independent review", encoding="utf-8")
    assert mission_state.main(["review-start", "M1", "--plan", str(root / "other.json")]) == 0
    with pytest.raises(verification.VerificationError, match="inputs"):
        verification.record_review(root, folder, "M1", receipt, root / "review.md", "reviewer", "unit", "clean")


def test_changed_review_start_record_is_rejected(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    path = review_with_start(mission, receipt)
    start = Path(receipt_data(path)["reviewStart"])
    start.write_text(start.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(verification.VerificationError, match="시작 기록"):
        verification.validate_review(folder, "M1", receipt, path)


def test_review_scope_diff_covers_add_modify_delete_and_skips_binary_and_large(mission):
    root, folder, plan = mission
    source = root / "src"
    source.mkdir()
    (source / "keep.txt").write_bytes(b"one\ntwo\nthree\n")
    (source / "gone.txt").write_text("bye\n", encoding="utf-8")
    (source / "data.bin").write_bytes(b"\0\1\2")
    (source / "huge.txt").write_text("x" * (verification.COPY_LIMIT + 1), encoding="utf-8")
    plan["inputs"] = ["src"]
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _, receipt = verify(mission)
    assert mission_state.main(start_args(mission)) == 0
    start = receipt_data(Path(receipt_data(folder / "verification/M1.review-start.latest.json")["reviewStart"]))
    assert start["copies"]["skipped"] == {"src/data.bin": "binary", "src/huge.txt": "large"}
    (source / "keep.txt").write_bytes(b"one\n2\nthree\nfour")
    (source / "gone.txt").unlink()
    (source / "new.txt").write_text("fresh\n", encoding="utf-8")
    (source / "data.bin").write_bytes(b"\0\3")
    _, receipt = verify(mission)
    review(mission, receipt)
    changed, diff = verification.review_scope_diff(root, folder, "M1", with_unit_brief(mission, receipt))
    assert changed == ["src/data.bin", "src/gone.txt", "src/keep.txt", "src/new.txt"]
    text = Path(diff).read_text(encoding="utf-8")
    assert "--- a/src/gone.txt\n+++ /dev/null\n" in text and "-bye\n" in text
    assert "--- /dev/null\n+++ b/src/new.txt\n" in text and "+fresh\n" in text
    assert "-two\n+2\n" in text and "+four\n\\ No newline at end of file\n" in text
    assert "비교 불가" in text and "src/data.bin" in text and "huge.txt" not in text


def test_review_scope_diff_compares_only_lines_changed_since_first_review(mission):
    root, folder, plan = mission
    source = root / "src"
    source.mkdir()
    (source / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    plan["inputs"] = ["src"]
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _, receipt = verify(mission)
    review_with_start(mission, receipt)
    (source / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    changed, diff = verification.review_scope_diff(root, folder, "M1", with_unit_brief(mission, receipt))
    assert changed == ["src/a.txt"]
    text = Path(diff).read_text(encoding="utf-8")
    assert "+gamma\n" in text and "-alpha" not in text and "+alpha" not in text


def test_review_scope_cli_prints_diff_path_and_old_records_get_list_only(mission, capsys):
    root, folder, _ = mission
    _, receipt = verify(mission)
    path = review(mission, receipt)
    (root / "src.py").write_text("value = 3\n", encoding="utf-8")
    capsys.readouterr()
    assert mission_state.main(["review-scope", "M1", "--plan", str(root / "plan.json")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith(": 1개") and lines[1] == "src.py" and lines[2].startswith("리뷰 변경 diff: ")
    assert "-value = 1" in Path(lines[2].split(": ", 1)[1]).read_text(encoding="utf-8")
    legacy = receipt_data(path)
    del legacy["copies"]
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert mission_state.main(["review-scope", "M1", "--plan", str(root / "plan.json")]) == 0
    assert capsys.readouterr().out.splitlines() == [lines[0], "src.py"]


def test_review_scope_diff_marks_only_unreadable_copy_and_keeps_list_and_other_diffs(mission):
    root, folder, plan = mission
    (root / "data.txt").write_text("d1\n", encoding="utf-8")
    plan["inputs"].append("data.txt")
    save_plan(mission)
    _, receipt = verify(mission)
    path = review(mission, receipt)
    record = receipt_data(path)
    (Path(record["copies"]["dir"]) / record["snapshot"]["src.py"]).write_text("tampered\n", encoding="utf-8")
    (root / "src.py").write_text("value = 4\n", encoding="utf-8")
    (root / "data.txt").write_text("d2\n", encoding="utf-8")
    plan = with_unit_brief(mission, receipt)
    changed, diff = verification.review_scope_diff(root, folder, "M1", plan)
    assert changed == ["data.txt", "src.py"]
    text = Path(diff).read_text(encoding="utf-8")
    assert "# 비교 불가(리뷰 시점 사본이 없거나 바뀜): src.py\n" in text
    assert "-d1\n+d2\n" in text and "-value = 1" not in text
    (Path(record["copies"]["dir"]) / record["snapshot"]["data.txt"]).unlink()
    changed, other = verification.review_scope_diff(root, folder, "M1", plan)
    assert changed == ["data.txt", "src.py"] and other != diff
    assert "# 비교 불가(리뷰 시점 사본이 없거나 바뀜): data.txt\n" in Path(other).read_text(encoding="utf-8")


def test_review_scope_diff_marks_unreadable_current_file(mission, monkeypatch):
    root, folder, _ = mission
    _, receipt = verify(mission)
    review(mission, receipt)
    (root / "src.py").write_text("value = 5\n", encoding="utf-8")
    real = Path.read_bytes

    def failing(self):
        if self.name == "src.py":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    changed, diff = verification.review_scope_diff(root, folder, "M1", with_unit_brief(mission, receipt))
    assert changed == ["src.py"]
    assert "# 비교 불가(" in Path(diff).read_text(encoding="utf-8")


def test_review_copy_skips_changed_and_non_text_files(mission):
    root, folder, _ = mission
    (root / "latin.txt").write_bytes("caf\xe9".encode("latin-1"))
    files = {"latin.txt": verification.file_hash(root / "latin.txt"), "src.py": "0" * 64, "absent.txt": "missing"}
    copies = verification.copy_snapshot_files(root, files, folder / "verification" / "copies")
    assert copies["files"] == [] and copies["skipped"] == {"latin.txt": "binary", "src.py": "changed"}


def findings_after_input_change(mission):
    root, folder, _ = mission
    _, first = verify(mission)
    assert mission_state.main(start_args(mission)) == 0
    (root / "src.py").write_text("value = 2\n", encoding="utf-8")
    _, second = verify(mission)
    findings = structured_review(mission, second, 1, [low()])
    assert receipt_data(findings)["inputChangedDuringReview"] == ["src.py"]
    return second


def test_closed_low_is_rejected_when_the_previous_review_saw_inputs_change(mission):
    root, folder, _ = mission
    second = findings_after_input_change(mission)
    pointer = latest_review_pointer(folder)
    with pytest.raises(verification.VerificationError, match="독립 재리뷰"):
        structured_review(mission, second, 2, [], ["F1"], "closed-low")
    assert latest_review_pointer(folder) == pointer
    clean = structured_review(mission, second, 3, [], ["F1"], "clean")
    verification.validate_review(folder, "M1", second, clean)


def test_complete_rejects_closed_low_after_a_review_that_saw_inputs_change(mission, monkeypatch):
    root, folder, _ = mission
    second = findings_after_input_change(mission)
    monkeypatch.setattr(verification, "require_closed_low", lambda *args: None)
    forged = structured_review(mission, second, 2, [], ["F1"], "closed-low")
    with pytest.raises(verification.VerificationError, match="독립 재리뷰"):
        verification.validate_review(folder, "M1", second, forged)
    assert mission_state.main(["complete", "M1", "--receipt", str(second), "--review", str(forged),
                               "--done-evidence", "checked"]) == 1


def test_closed_low_is_rejected_for_a_finding_first_recorded_as_regression(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low()])
    structured_review(mission, receipt, 2, [], ["F1"], "clean")
    structured_review(mission, receipt, 3, [low("F3", origin="regression", relatedId="F1")])
    structured_review(mission, receipt, 4, [low("F3", origin="unresolved")])
    pointer = latest_review_pointer(folder)
    with pytest.raises(verification.VerificationError, match="회귀가 아닌"):
        structured_review(mission, receipt, 5, [], ["F3"], "closed-low")
    assert latest_review_pointer(folder) == pointer


@pytest.mark.parametrize("later", [{"severity": "low"}, {"severity": "medium"}, {}])
def test_same_finding_id_cannot_lower_or_drop_severity_between_rounds(mission, later):
    root, folder, _ = mission
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [finding(severity="high")])
    pointer = latest_review_pointer(folder)
    with pytest.raises(verification.VerificationError, match="severity"):
        structured_review(mission, receipt, 2, [finding(origin="unresolved", **later)])
    assert latest_review_pointer(folder) == pointer
    kept = structured_review(mission, receipt, 3, [finding(origin="unresolved", severity="high")])
    raised = structured_review(mission, receipt, 4, [finding(origin="unresolved", severity="critical")])
    assert receipt_data(kept)["findings"][0]["severity"] == "high"
    assert receipt_data(raised)["findings"][0]["severity"] == "critical"


def test_round_three_diagnostic_is_not_attached_to_passing_reviews(mission):
    _, receipt = verify(mission)
    structured_review(mission, receipt, 1, [low()])
    structured_review(mission, receipt, 2, [low(origin="unresolved")])
    closed = structured_review(mission, receipt, 3, [], ["F1"], "closed-low")
    assert receipt_data(closed)["round"] == 3 and receipt_data(closed)["diagnostics"] == []
    again = structured_review(mission, receipt, 4, [low("F4", origin="new")])
    assert any("closed-low" in line for line in receipt_data(again)["diagnostics"])


@pytest.mark.parametrize("failure", ["write", "mkdir"])
def test_unwritable_review_copies_are_skipped_instead_of_failing_the_review(mission, monkeypatch, failure):
    root, folder, _ = mission
    _, receipt = verify(mission)
    real_write, real_mkdir = Path.write_bytes, Path.mkdir

    def guarded_write(self, data):
        if failure == "write" and ".copies" in str(self):
            raise OSError(36, "File name too long")
        return real_write(self, data)

    def guarded_mkdir(self, *args, **kwargs):
        if failure == "mkdir" and self.name.endswith(".copies"):
            raise OSError(36, "File name too long")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", guarded_write)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    assert mission_state.main(start_args(mission)) == 0
    start = receipt_data(Path(receipt_data(folder / "verification/M1.review-start.latest.json")["reviewStart"]))
    expected = {"check.py": "unwritable", "src.py": "unwritable"}
    assert start["copies"]["files"] == [] and start["copies"]["skipped"] == expected
    checked = receipt_data(review(mission, receipt))
    assert checked["copies"]["files"] == [] and checked["copies"]["skipped"] == expected
    store = folder / "verification" / "M1.copies"
    assert not store.exists() or list(store.iterdir()) == []


def test_review_copies_are_deduplicated_by_content_across_rounds_and_keys(mission):
    root, folder, _ = mission
    _, receipt = verify(mission)
    assert mission_state.main(start_args(mission)) == 0
    review(mission, receipt)
    review(mission, receipt)
    store = folder / "verification" / "M1.copies"
    assert sorted(path.name for path in store.iterdir()) == sorted(set(receipt_data(receipt)["snapshot"].values()))
    (root / "twin-a.txt").write_text("same\n", encoding="utf-8")
    (root / "twin-b.txt").write_text("same\n", encoding="utf-8")
    files = {name: verification.file_hash(root / name) for name in ("twin-a.txt", "twin-b.txt")}
    copies = verification.copy_snapshot_files(root, files, folder / "verification" / "twins")
    assert copies["files"] == ["twin-a.txt", "twin-b.txt"] and len(list(Path(copies["dir"]).iterdir())) == 1


def test_review_copy_total_is_capped_and_the_rest_is_marked_budget(mission):
    root, folder, _ = mission
    assert verification.COPY_BUDGET == 64 * 1024 * 1024
    for name in ("one.txt", "two.txt"):
        (root / name).write_text("123456", encoding="utf-8")
    (root / "two.txt").write_text("654321", encoding="utf-8")
    files = {name: verification.file_hash(root / name) for name in ("one.txt", "two.txt")}
    copies = verification.copy_snapshot_files(root, files, folder / "verification" / "capped", budget=10)
    assert copies["files"] == ["one.txt"] and copies["skipped"] == {"two.txt": "budget"}


def inputs_plan(mission):
    root, _, plan = mission
    (root / "a.txt").write_text("a1\n", encoding="utf-8")
    (root / "b.txt").write_text("b1\n", encoding="utf-8")
    (root / "step.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "name = sys.argv[1]\n"
        "counter = Path('runs_' + name)\n"
        "counter.write_text(counter.read_text() + 'x' if counter.exists() else 'x')\n"
        "Path('out_' + name).mkdir(exist_ok=True)\n"
        "Path('out_' + name, 'result.json').write_text('{\"status\":\"passed\"}')\n", encoding="utf-8")
    plan.update(inputs=["a.txt", "b.txt"], commands=[
        {"id": name, "argv": [sys.executable, "step.py", name], "reusable": True, "outputs": [f"out_{name}"],
         "inputs": [f"{name}.txt"]} for name in ("a", "b")
    ], results=[{"path": f"out_{name}/result.json", "command": name, "equals": {"status": "passed"}}
                for name in ("a", "b")])
    save_plan(mission)
    return plan


def step_statuses(path):
    return [step["status"] for step in receipt_data(path)["steps"]]


def runs(root, name):
    return (root / f"runs_{name}").read_text(encoding="utf-8")


def test_declared_step_inputs_reuse_untouched_steps_and_rerun_later_steps_after_an_earlier_rerun(mission):
    root, _, _ = mission
    inputs_plan(mission)
    _, first = verify(mission)
    assert step_statuses(first) == ["passed", "passed"]
    (root / "b.txt").write_text("b2\n", encoding="utf-8")
    _, second = verify(mission)
    assert step_statuses(second) == ["reused", "passed"]
    assert runs(root, "a") == "x" and runs(root, "b") == "xx"
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    _, third = verify(mission)
    assert step_statuses(third) == ["passed", "passed"]
    assert runs(root, "a") == "xx" and runs(root, "b") == "xxx"


@pytest.mark.parametrize("declared", [True, False])
def test_step_without_inputs_keeps_the_whole_snapshot_in_its_key(mission, declared):
    root, _, _ = mission
    plan = inputs_plan(mission)
    plan["commands"].reverse()
    if not declared:
        del plan["commands"][0]["inputs"]
    save_plan(mission)
    verify(mission)
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    _, second = verify(mission)
    expected = ["reused", "passed"]
    if not declared:
        expected = ["passed", "passed"]
    assert step_statuses(second) == expected


def test_step_inputs_accept_folder_and_glob_declarations(mission):
    root, _, _ = mission
    plan = inputs_plan(mission)
    (root / "data/deep").mkdir(parents=True)
    (root / "data/deep/x.txt").write_text("x\n", encoding="utf-8")
    (root / "data/y.md").write_text("y\n", encoding="utf-8")
    plan["inputs"] = ["a.txt", "b.txt", "data"]
    for inputs in (["data"], ["data/**/*.txt", "data/*.md"], ["a.txt", "**/x.txt"]):
        plan["commands"][0]["inputs"] = inputs
        verification.validate_plan(root, plan, ["pytest-pass"])
    before = verification.snapshot(root, plan["inputs"])
    plan["commands"][0]["inputs"] = ["data/**/*.txt"]
    assert sorted(verification.step_inputs(root, plan["commands"][0], before)) == ["data/deep/x.txt"]
    plan["commands"][0]["inputs"] = ["data"]
    assert sorted(verification.step_inputs(root, plan["commands"][0], before)) == ["data/deep/x.txt", "data/y.md"]
    del plan["commands"][0]["inputs"]
    assert verification.step_inputs(root, plan["commands"][0], before) == before


@pytest.mark.parametrize("declared", [
    [], "a.txt", [""], [1], ["other.txt"], ["../a.txt"], ["/abs/a.txt"], ["C:/a.txt"], ["."], ["a.txt\n"],
])
def test_malformed_or_outside_command_inputs_are_rejected_on_every_load(mission, declared):
    root, _, _ = mission
    plan = inputs_plan(mission)
    (root / "other.txt").write_text("outside\n", encoding="utf-8")
    plan["commands"][0]["inputs"] = declared
    with pytest.raises(verification.VerificationError):
        verification.validate_plan(root, plan, ["pytest-pass"])
    save_plan(mission)
    code, path = verify(mission)
    assert code == 1 and receipt_data(path)["failure"]["category"] == "plan_invalid"
    assert mission_state.main(start_args(mission)) == 1
    assert mission_state.main(["review-scope", "M1", "--plan", str(root / "plan.json")]) == 1


@pytest.mark.parametrize("declared", [["missing/**"], ["*.md"], ["a.txt", "nested/*.txt"]])
def test_command_inputs_matching_no_plan_input_file_fail_only_the_preflight(mission, declared):
    root, _, _ = mission
    plan = inputs_plan(mission)
    plan["commands"][0]["inputs"] = declared
    save_plan(mission)
    assert verification.validate_plan(root, plan, ["pytest-pass"]) is plan
    with pytest.raises(verification.VerificationError, match="일치하지 않음"):
        verification.validate_plan(root, plan, ["pytest-pass"], require_matches=True)
    checked = verification.preflight(root, plan, allow_command, require_matches=True)
    assert checked["status"] == "fail" and checked["failure"]["category"] == "plan_invalid"
    assert verification.preflight(root, plan, allow_command)["status"] == "pass"
    assert mission_state.main(["preflight", "M1", "--plan", str(root / "plan.json")]) == 1
    assert verify(mission)[0] == 0


def declared_data_plan(mission, declared):
    root, _, plan = mission
    inputs_plan(mission)
    (root / "data/deep").mkdir(parents=True)
    (root / "data/deep/x.txt").write_text("x\n", encoding="utf-8")
    plan["inputs"] = ["a.txt", "b.txt", "data"]
    plan["commands"][0]["inputs"] = declared
    save_plan(mission)


def test_deleting_every_file_a_step_declares_keeps_the_same_plan_verifiable_and_the_scope_diff(mission, capsys):
    root, _, _ = mission
    declared_data_plan(mission, ["data/**/*.txt", "data/*.md"])
    _, first = verify(mission)
    assert step_statuses(first) == ["passed", "passed"]
    review_with_start(mission, first)
    (root / "data/deep/x.txt").unlink()
    code, second = verify(mission)
    assert code == 0 and step_statuses(second) == ["passed", "passed"]
    assert runs(root, "a") == "xx"
    plan_path = str(root / "plan.json")
    capsys.readouterr()
    assert mission_state.main(["review-scope", "M1", "--plan", plan_path]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith(": 1개") and lines[1] == "data/deep/x.txt" and lines[2].startswith("리뷰 변경 diff: ")
    assert "-x\n" in Path(lines[2].split(": ", 1)[1]).read_text(encoding="utf-8")
    assert mission_state.main(["review-start", "M1", "--plan", plan_path]) == 0
    assert mission_state.main(["preflight", "M1", "--plan", plan_path]) == 1


@pytest.mark.parametrize("declared", [["data"], ["data/**/*.txt"]])
def test_adding_or_deleting_files_under_a_declared_folder_or_glob_reruns_the_step(mission, declared):
    root, _, _ = mission
    declared_data_plan(mission, declared)
    verify(mission)
    (root / "data/deep/new.txt").write_text("n\n", encoding="utf-8")
    assert step_statuses(verify(mission)[1]) == ["passed", "passed"]
    assert runs(root, "a") == "xx"
    (root / "data/deep/new.txt").unlink()
    assert step_statuses(verify(mission)[1]) == ["passed", "passed"]
    assert runs(root, "a") == "xxx"


def test_adding_a_file_outside_the_declared_glob_reuses_the_step(mission):
    root, _, _ = mission
    declared_data_plan(mission, ["data/**/*.txt"])
    verify(mission)
    (root / "data/y.md").write_text("y\n", encoding="utf-8")
    assert step_statuses(verify(mission)[1]) == ["reused", "reused"]
    assert runs(root, "a") == "x"


def test_glob_matching_ignores_case_only_where_path_inputs_do(monkeypatch):
    monkeypatch.setattr(verification, "glob_ignores_case", lambda: True)
    assert verification.input_matcher("glob", "src/**/*.cs")("src/A/Foo.CS") is True
    monkeypatch.setattr(verification, "glob_ignores_case", lambda: False)
    assert verification.input_matcher("glob", "src/**/*.cs")("src/A/Foo.CS") is False
    assert verification.input_matcher("glob", "src/**/*.cs")("src/A/Foo.cs") is True


def test_review_scope_keeps_the_list_when_the_diff_cannot_be_written(mission, capsys, monkeypatch):
    root, _, _ = mission
    _, receipt = verify(mission)
    review(mission, receipt)
    (root / "src.py").write_text("value = 3\n", encoding="utf-8")
    real = Path.write_bytes

    def failing(self, data):
        if self.suffix == ".diff":
            raise PermissionError("denied")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing)
    capsys.readouterr()
    assert mission_state.main(["review-scope", "M1", "--plan", str(root / "plan.json")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:] == ["src.py", "diff 쓰기 실패: denied"] and lines[0].endswith(": 1개")


@pytest.mark.parametrize("pattern,key,expected", [
    ("src/**/*.py", "src/a.py", True), ("src/**/*.py", "src/x/y/a.py", True), ("src/**/*.py", "src/a.txt", False),
    ("src/*.py", "src/x/a.py", False), ("data/?.txt", "data/a.txt", True), ("data/?.txt", "data/ab.txt", False),
    ("data/[ab].txt", "data/b.txt", True), ("data/[!ab].txt", "data/b.txt", False),
    ("data/[!ab].txt", "data/c.txt", True), ("**", "any/thing.txt", True), ("src/**", "src/a/b", True),
    ("a.*", "a.b", True), ("a.*", "aXb", False), ("a[", "a[", True),
])
def test_input_glob_matching_follows_path_segments(pattern, key, expected):
    assert verification.input_matcher("glob", pattern)(key) is expected


def test_fix_round_reuses_untouched_steps_keeps_scope_and_completes_through_closed_low(mission, capsys):
    root, folder, _ = mission
    inputs_plan(mission)
    _, first = verify(mission)
    assert mission_state.main(start_args(mission)) == 0
    findings = structured_review(mission, first, 1, [low()])
    assert "inputChangedDuringReview" not in receipt_data(findings)
    (root / "b.txt").write_text("b2\n", encoding="utf-8")
    code, second = verify(mission)
    assert code == 0 and step_statuses(second) == ["reused", "passed"]
    assert receipt_data(second)["executionCounts"] == {"executed": 1, "reused": 1}
    capsys.readouterr()
    assert mission_state.main(["review-scope", "M1", "--plan", str(root / "plan.json")]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith(": 1개") and lines[1] == "b.txt" and lines[2].startswith("리뷰 변경 diff: ")
    diff = Path(lines[2].split(": ", 1)[1]).read_text(encoding="utf-8")
    assert "-b1\n" in diff and "+b2\n" in diff
    closed = structured_review(mission, second, 2, [], ["F1"], "closed-low")
    assert receipt_data(closed)["status"] == "closed-low"
    assert mission_state.main(["complete", "M1", "--receipt", str(second), "--review", str(closed),
                               "--done-evidence", "checked fix diff"]) == 0
    assert (folder / "units.tsv").read_text(encoding="utf-8").rstrip().endswith("\tdone")
