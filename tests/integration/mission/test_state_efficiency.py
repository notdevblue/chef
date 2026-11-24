"""Fictional mission fixtures for contract/evidence efficiency contracts."""

import json
import sys
from pathlib import Path

import pytest

from chef.mission import state
from chef.mission import unit_contracts
from chef.verification import references


@pytest.fixture
def sample(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    adapter = root / ".claude/orchestrator"
    adapter.mkdir(parents=True)
    (adapter / "adapter.json").write_text(json.dumps({
        "schema": 1, "paths": {"stateDir": "state"},
        "verify": {"grades": ["sample-pass"]}, "guard": {},
    }), encoding="utf-8")
    (root / "state").mkdir()
    (root / "state/ACTIVE").write_text('{"id":"sample","session":"fictional-session"}')
    monkeypatch.chdir(root)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "none"))

    def run(*args):
        capsys.readouterr()
        code = state.main(["--project", str(root), *args])
        output = capsys.readouterr()
        return code, output.out.strip(), output.err.strip()

    assert run("init")[0] == 0
    assert run("unit", "add", "U1", "--goal", "Check widget", "--files", "widget.py",
               "--done", "Expected value", "--verify", "sample checks", "--timebox", "30",
               "--forbid", "publish")[0] == 0
    assert run("unit", "set", "U1", "--status", "doing")[0] == 0
    (root / "widget.py").write_text("value = 7\n")
    (root / "plan.json").write_text(json.dumps({"grade": "sample-pass", "inputs": ["widget.py"], "environment": {"target": "fictional"},
        "commands": [{"argv": [sys.executable, "-c", "pass"]}]}))
    return root, root / "state/sample", run


def receipt_path(folder):
    return Path(json.loads((folder / "verification/U1.latest.json").read_text())["receipt"])


def test_effective_noop_preserves_all_mission_files(sample):
    root, folder, run = sample
    assert run("verify", "U1", "--plan", "plan.json")[0] == 0
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.rglob("*") if path.is_file()}
    code, out, err = run("unit", "set", "U1", "--goal", " Check widget ", "--timebox", "030", "--status", "doing")
    assert code == 0 and "no-op" in out and not err
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.rglob("*") if path.is_file()}
    assert not (folder / "contracts").exists()


def test_contract_edit_preserves_pass_and_reports_reverification(sample):
    root, folder, run = sample
    assert run("verify", "U1", "--plan", "plan.json")[0] == 0
    receipt = receipt_path(folder)
    preserved = receipt.read_bytes()
    assert run("ledger", "record", "U1", "--status", "pass", "--grade", "sample-pass",
               "--evidence", "fictional recorded pass")[0] == 0
    ledger = (folder / "ledger.tsv").read_bytes()
    code, output, _ = run("unit", "set", "U1", "--done", "Updated expected value")
    assert code == 0
    change = json.loads(output.splitlines()[-1])["contractChange"]
    assert change["reverificationNeeded"] is True
    assert change["old"]["version"] == 1 and change["new"]["version"] == 2
    assert change["old"]["sha256"] != change["new"]["sha256"]
    assert change["previousReceipt"] == str(receipt)
    assert receipt.read_bytes() == preserved and (folder / "ledger.tsv").read_bytes() == ledger
    assert "reverification needed" in run("status")[1]
    assert json.loads(run("brief", "U1")[1])["contract"]["reverificationNeeded"] is True
    assert run("verify", "U1", "--plan", "plan.json")[0] == 0
    latest = receipt_path(folder)
    assert latest != receipt
    assert json.loads(latest.read_text())["plan"]["unitContract"] == change["new"]
    assert json.loads(run("brief", "U1")[1])["contract"]["reverificationNeeded"] is False


def test_edit_then_revert_does_not_resurrect_old_receipt(sample, monkeypatch):
    root, folder, run = sample
    assert run("verify", "U1", "--plan", "plan.json")[0] == 0
    old = receipt_path(folder)
    assert run("unit", "set", "U1", "--done", "Other value")[0] == 0
    assert run("unit", "set", "U1", "--done", "Expected value")[0] == 0
    monkeypatch.setattr(state.verification, "validate_receipt", lambda *a, **k: None)
    monkeypatch.setattr(state.verification, "validate_latest", lambda *a, **k: None)
    code, _, err = run("complete", "U1", "--receipt", str(old), "--review", "unused", "--done-evidence", "sample")
    assert code == 1 and "contract version" in err
    saved = json.loads((folder / "contracts/U1.json").read_text())
    assert saved["current"]["version"] == 3 and len(saved["history"]) == 2


def write_evidence(root, files):
    (root / "evidence.json").write_text(json.dumps({"schema": 1, "files": files}))


def test_ledger_structured_and_legacy_evidence_audit_is_read_only_and_bounded(sample):
    root, folder, run = sample
    (root / "report.log").write_text("fictional check completed")
    (root / "result.json").write_text('{"result":"fictional"}')
    write_evidence(root, [{"path": "report.log", "kind": "log"}, {"path": "result.json", "kind": "report"}])
    assert run("ledger", "record", "U1", "--status", "pass", "--grade", "sample-pass",
               "--evidence-input", "evidence.json", "--evidence", "Two fictional checks")[0] == 0
    assert run("ledger", "record", "U1", "--status", "retry", "--grade", "sample-pass", "--evidence", "old note")[0] == 0
    code, output, _ = run("ledger", "audit")
    result = json.loads(output)
    assert code == 0 and result["semanticValidity"] == "not_checked"
    assert result["counts"]["legacyRows"] == 1 and result["counts"]["checkedFiles"] == 2
    (root / "report.log").unlink()
    (root / "result.json").write_text("changed")
    before = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    code, output, _ = run("ledger", "audit", "--limit", "1")
    result = json.loads(output)
    assert code == 1 and result["counts"]["missing"] == result["counts"]["changed"] == 1
    assert len(result["issues"]) == 1 and result["omitted"] == 1
    assert before == {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}


@pytest.mark.parametrize("item", [{"path": "absent", "kind": "log"}, {"path": "widget.py", "kind": ""},
    {"path": "widget.py", "kind": "file", "sha256": "0" * 64}, {"path": "../outside", "kind": "file"}])
def test_evidence_validation_rejects_without_ledger_write(sample, item):
    root, folder, run = sample
    before = (folder / "ledger.tsv").read_bytes()
    write_evidence(root, [item])
    assert run("ledger", "record", "U1", "--status", "pass", "--grade", "sample-pass", "--evidence-input", "evidence.json")[0] == 1
    assert (folder / "ledger.tsv").read_bytes() == before


def test_frozen_reference_keeps_immutable_content_and_reports_live_drift(sample):
    root, _, run = sample
    (root / "guide.md").write_text("Fictional rules version one\n")
    code, output, _ = run("reference", "snapshot", "--source", "guide.md", "--output", "references.json", "--freeze")
    assert code == 0
    source = json.loads(output)["sources"][0]
    assert source["path"] == "guide.md" and source["frozen"]["sha256"] == source["sha256"]
    frozen = root / source["frozen"]["path"]
    preserved = frozen.read_bytes()
    (root / "guide.md").write_text("Fictional rules version two\n")
    checked = references.check(root, "references.json")
    assert checked["status"] == "pass" and checked["liveSources"]["changed"] == ["guide.md"]
    assert frozen.read_bytes() == preserved
    old_hash = references.file_pointer(root, "references.json")["sha256"]
    updated = references.snapshot(root, ["guide.md"], "references.json", expected_sha256=old_hash, freeze=True)
    assert updated["sources"][0]["frozen"]["path"] != source["frozen"]["path"]
    assert frozen.read_bytes() == preserved
    with pytest.raises(references.ReferenceError, match="expect-sha256"):
        references.snapshot(root, ["guide.md"], "references.json", expected_sha256=old_hash, freeze=True)


def test_frozen_object_corruption_or_escape_is_rejected(sample):
    root, _, _ = sample
    bundle = references.snapshot(root, ["widget.py"], "refs.json", freeze=True)
    source = bundle["sources"][0]
    frozen = root / source["frozen"]["path"]
    frozen.write_text("corrupt")
    assert references.check(root, "refs.json")["status"] == "fail"
    with pytest.raises(references.ReferenceError, match="hash"):
        references.snapshot(root, ["widget.py"], "second.json", freeze=True)
    source["frozen"]["path"] = "../outside"
    (root / "refs.json").write_text(json.dumps(bundle))
    with pytest.raises(references.ReferenceError):
        references.load(root, "refs.json")


def test_coordinator_ineligible_does_not_create_receipt_or_execute(sample, monkeypatch):
    root, folder, run = sample
    def forbidden(*args, **kwargs):
        pytest.fail("ineligible coordinator executed commands")
    monkeypatch.setattr(state.verification, "run", forbidden)
    code, _, err = run("verify", "U1", "--plan", "plan.json", "--coordinator")
    assert code == 1 and err
    assert not list((folder / "verification").glob("*.json"))


def review_args(root, *, text="print('fictional review output')"):
    from chef.verification.contracts import digest
    from chef.verification.plan import snapshot
    python = Path(sys.prefix) / ("python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        python = Path(sys.executable)
    (root / "scope.json").write_text('["widget.py"]')
    (root / "findings.json").write_text('[]')
    (root / "reviewer.py").write_text("from pathlib import Path\np=Path('review-count')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n" + text + "\n")
    return ["review-run", "U1", "--plan", "plan.json", "--snapshot", digest(snapshot(root, ["widget.py"])),
            "--purpose", "fictional independent inspection", "--scope", "scope.json", "--findings", "findings.json",
            "--", str(python), "reviewer.py"]


def test_review_run_reuses_completed_job_without_clean_verdict(sample):
    root, folder, run = sample
    args = review_args(root)
    code, output, err = run(*args)
    assert code == 0, (output, err)
    first = json.loads(output)
    assert first["dispatched"] is True and first["reviewVerdict"] == "not_recorded"
    assert first["result"]["status"] == "completed"
    assert not (folder / "verification/U1.review.latest.json").exists()
    code, output, err = run(*args)
    assert code == 0, err
    second = json.loads(output)
    assert second["dispatched"] is False and second["job"]["key"] == first["job"]["key"]
    assert (root / "review-count").read_text() == "x"


def test_review_reuse_requires_unchanged_output_hash(sample):
    root, folder, run = sample
    args = review_args(root)
    code, output, err = run(*args)
    assert code == 0, err
    result = json.loads(output)
    log = folder / result["result"]["stdout"]["file"]["path"]
    log.write_text("tampered")
    code, _, err = run(*args)
    assert code == 1 and "changed" in err
    assert (root / "review-count").read_text() == "x"


def test_review_snapshot_and_tool_changes_cannot_reuse_previous_job(sample):
    root, _, run = sample
    args = review_args(root)
    assert run(*args)[0] == 0
    (root / "widget.py").write_text("value = 8\n")
    code, _, err = run(*args)
    assert code == 1 and "stale" in err
    assert (root / "review-count").read_text() == "x"
    (root / "widget.py").write_text("value = 7\n")
    (root / "reviewer.py").write_text((root / "reviewer.py").read_text() + "print('changed tool')\n")
    code, output, err = run(*args)
    assert code == 0, err
    assert json.loads(output)["dispatched"] is True
    assert (root / "review-count").read_text() == "xx"


def test_review_failed_dispatch_is_never_clean_and_retains_result(sample):
    root, folder, run = sample
    args = review_args(root, text="raise SystemExit(4)")
    code, output, err = run(*args)
    assert code == 1 and not err
    result = json.loads(output)
    assert result["result"]["exitCode"] == 4 and result["reviewVerdict"] == "not_recorded"
    code, output, _ = run(*args)
    assert code == 1 and json.loads(output)["dispatched"] is False
    assert (root / "review-count").read_text() == "x"
    assert not (folder / "verification/U1.review.latest.json").exists()


def test_review_bounded_log_pointers(sample, monkeypatch):
    from chef.mission import review_dispatch
    root, folder, run = sample
    monkeypatch.setattr(review_dispatch, "MAX_LOG_BYTES", 64)
    args = review_args(root, text="print('x'*1000)")
    code, output, err = run(*args)
    assert code == 0, err
    log = json.loads(output)["result"]["stdout"]
    assert log["truncated"] is True and log["bytes"] == 64
    assert (folder / log["file"]["path"]).stat().st_size == 64
    assert "x" * 100 not in output


def test_review_duplicate_running_job_does_not_dispatch(sample, monkeypatch):
    from chef.mission import review_dispatch
    root, _, run = sample
    args = review_args(root)
    def reserve(*_args, **_kwargs):
        return {"key": "a" * 64, "token": "fictional-token", "pid": 123,
                "status": "running", "acquired": False}
    monkeypatch.setattr(review_dispatch.execution, "reserve_review", reserve)
    code, output, err = run(*args)
    assert code == 0, err
    result = json.loads(output)
    assert result["status"] == "running" and result["dispatched"] is False
    assert not (root / "review-count").exists()


def test_unit_mutation_and_completion_reject_active_execution(sample):
    from chef.verification import execution
    root, folder, run = sample
    before = (folder / "units.tsv").read_bytes()
    with execution.unit_execution_lock(folder, "U1"):
        code, output, err = run("unit", "set", "U1", "--done", "New condition")
        assert code == 1 and not err
        assert json.loads(output)["category"] == "execution_busy"
        code, output, _ = run("complete", "U1", "--receipt", "unused", "--review", "unused", "--done-evidence", "fictional")
        assert code == 1 and json.loads(output)["category"] == "execution_busy"
    assert (folder / "units.tsv").read_bytes() == before
    assert not (folder / "contracts/U1.json").exists()


def test_unit_write_failure_rolls_back_contract_sidecar(sample, monkeypatch):
    root, folder, run = sample
    before = (folder / "units.tsv").read_bytes()
    def fail_write(*_args):
        raise OSError("fictional storage failure")
    monkeypatch.setattr(state, "write_table", fail_write)
    code, _, err = run("unit", "set", "U1", "--goal", "Other goal")
    assert code == 1 and "storage failure" in err
    assert (folder / "units.tsv").read_bytes() == before
    assert not (folder / "contracts/U1.json").exists()


@pytest.mark.parametrize("stage", ["before-unit", "after-unit"])
@pytest.mark.parametrize("existing", [False, True])
def test_contract_journal_survives_actual_process_kill(sample, stage, existing):
    import os
    import queue
    import subprocess
    import threading
    root, folder, run = sample
    if existing:
        assert run("unit", "set", "U1", "--goal", "Earlier goal")[0] == 0
    old_brief = json.loads(run("brief", "U1")[1])
    script = '''
import sys, threading
from chef.mission import state, unit_contracts
stage, project = sys.argv[1:]
def pause():
    print("transition-ready", flush=True)
    threading.Event().wait(30)
if stage == "before-unit":
    original = unit_contracts.save
    def save(folder, row, value):
        original(folder, row, value)
        if "transition" in value:
            pause()
    unit_contracts.save = save
else:
    original = state.write_table
    def write(*args):
        original(*args)
        pause()
    state.write_table = write
state.main(["--project", project, "unit", "set", "U1", "--goal", "Interrupted goal"])
'''
    env = {**os.environ, "PYTHONPATH": str(Path(state.__file__).parents[2])}
    child = subprocess.Popen([sys.executable, "-c", script, stage, str(root)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
    try:
        assert ready.get(timeout=10).strip() == "transition-ready"
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()
    code, output, err = run("brief", "U1")
    assert code == 0, err
    current = json.loads(output)
    assert current["goal"] == (old_brief["goal"] if stage == "before-unit" else "Interrupted goal")
    assert run("unit", "set", "U1", "--goal", "Recovered goal")[0] == 0
    saved = json.loads((folder / "contracts/U1.json").read_text())
    assert "transition" not in saved
    assert json.loads(run("brief", "U1")[1])["goal"] == "Recovered goal"


def test_review_bundle_recapture_at_same_path_changes_request_identity(sample):
    root, _, run = sample
    (root / "guide.md").write_text("Fictional guide")
    references.snapshot(root, ["guide.md"], "references.json", revision="one")
    plan = json.loads((root / "plan.json").read_text())
    plan["referenceBundle"] = "references.json"
    (root / "plan.json").write_text(json.dumps(plan))
    args = review_args(root)
    code, output, err = run(*args)
    assert code == 0, err
    old = json.loads(output)["job"]["key"]
    expected = references.file_pointer(root, "references.json")["sha256"]
    references.snapshot(root, ["guide.md"], "references.json", revision="two", expected_sha256=expected)
    code, output, err = run(*args)
    assert code == 0, err
    result = json.loads(output)
    assert result["dispatched"] is True and result["job"]["key"] != old
    assert (root / "review-count").read_text() == "xx"


def test_reference_read_targets_separate_frozen_pointer_and_provenance(sample):
    root, _, run = sample
    (root / "guide.md").write_text("Fictional frozen guidance")
    references.snapshot(root, ["guide.md"], "references.json", freeze=True)
    (root / "guide.md").write_text("Changed live guidance")
    for output in (run("reference", "show", "--input", "references.json")[1],
                   run("brief", "U1", "--references", "references.json")[1]):
        read = json.loads(output)["readSources"][0]
        assert read["mode"] == "frozen"
        assert read["path"].startswith(".reference-objects/sha256/")
        assert read["provenance"]["path"] == "guide.md"
        assert (root / read["path"]).read_text() == "Fictional frozen guidance"


def test_coordinator_explicit_path_always_executes_new_checks(sample):
    from chef.verification import plan as plans
    root, folder, run = sample
    python = Path(sys.prefix) / ("python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        python = Path(sys.executable)
    (root / "tiny-check.py").write_text("from pathlib import Path\np=Path('tiny-runs')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n")
    command = {"argv": [str(python), "tiny-check.py"], "cwd": ".", "reusable": True,
               "inputs": ["widget.py", "tiny-check.py"], "outputs": []}
    plan = {"grade": "sample-pass", "inputs": ["widget.py", "tiny-check.py"],
            "environment": {"target": "fictional"}, "commands": [command]}
    (root / "plan.json").write_text(json.dumps(plan))
    adapter_file = root / ".claude/orchestrator/adapter.json"
    adapter = json.loads(adapter_file.read_text())
    adapter["verify"]["coordinatorChecks"] = [{"grade": "sample-pass", "commands": [
        {"argv": command["argv"], "cwd": ".", "tools": plans.command_runtime(root, command)["tools"]}]}]
    adapter_file.write_text(json.dumps(adapter))
    for _ in range(2):
        code, output, err = run("verify", "U1", "--plan", "plan.json", "--coordinator")
        assert code == 0, err
        receipt = json.loads(receipt_path(folder).read_text())
        assert receipt["executionCounts"]["executed"] == 1 and receipt["executionCounts"]["reused"] == 0
    assert (root / "tiny-runs").read_text() == "xx"


def test_review_bundle_change_during_dispatch_cannot_pass(sample):
    root, _, run = sample
    (root / "guide.md").write_text("Fictional guide")
    references.snapshot(root, ["guide.md"], "references.json", revision="one")
    plan = json.loads((root / "plan.json").read_text())
    plan["referenceBundle"] = "references.json"
    (root / "plan.json").write_text(json.dumps(plan))
    args = review_args(root, text="import json\np=Path('references.json')\nb=json.loads(p.read_text())\nb['sources'][0]['revision']='two'\np.write_text(json.dumps(b))")
    code, output, err = run(*args)
    assert code == 1, (output, err)
    result = json.loads(output)
    assert result["result"]["exitCode"] == 0 and result["result"]["inputsUnchanged"] is False
    assert result["result"]["status"] == "failed" and result["reviewVerdict"] == "not_recorded"


def test_contract_journal_retained_if_writer_raises_after_replacing_tsv(sample, monkeypatch):
    root, folder, run = sample
    original = state.write_table
    def after_replace(*args):
        original(*args)
        raise OSError("fictional post-replace failure")
    monkeypatch.setattr(state, "write_table", after_replace)
    code, _, err = run("unit", "set", "U1", "--goal", "Committed before error")
    assert code == 1 and "post-replace" in err
    code, output, err = run("brief", "U1")
    assert code == 0, err
    assert json.loads(output)["goal"] == "Committed before error"
    assert "transition" in json.loads((folder / "contracts/U1.json").read_text())
