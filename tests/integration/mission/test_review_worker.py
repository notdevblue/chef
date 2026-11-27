"""Subprocess lifetime tests using newly invented widget review fixtures."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chef.locations import PLUGIN_ROOT
from chef.infrastructure import configuration
from chef.infrastructure.fingerprints import implementation_digest
from chef.infrastructure import process as infrastructure_process
from chef.mission import review_dispatch, review_worker, state
from chef.mission.contracts import EDITABLE_FIELDS, UNIT_HEADER
from chef.mission.storage import read_table
from chef.verification import execution, plan as plans, references
from chef.verification.contracts import VerificationError, digest

ENTRY = PLUGIN_ROOT / "skills/mission/scripts/mission_state.py"
PACKAGE = PLUGIN_ROOT


def python_binary():
    # The Windows Store alias can launch Python but cannot pin executable bytes.
    candidate = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
    if not candidate.is_file():
        pytest.skip("hashable Python executable unavailable")
    return str(candidate)


def eventually(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("subprocess did not reach the expected observable checkpoint")


@pytest.fixture
def sample(tmp_path, monkeypatch, capsys):
    home, root = tmp_path / "home", tmp_path / "widget-project"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHONPATH", str(PACKAGE))
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty-adapters"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    adapter = {"schema": 1, "paths": {"stateDir": "state"},
               "verify": {"grades": ["widget-check"]}, "guard": {}}
    configuration_dir = root / ".claude/orchestrator"
    configuration_dir.mkdir(parents=True)
    (configuration_dir / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    (root / "state").mkdir()
    (root / "state/ACTIVE").write_text('{"id":"widget","session":"fictional-session"}', encoding="utf-8")
    monkeypatch.chdir(root)
    for arguments in (("init",), ("unit", "add", "U1", "--goal", "Inspect widget", "--files", "widget.py",
            "--done", "Expected value", "--verify", "widget check", "--timebox", "30", "--forbid", "publish")):
        assert state.main(["--project", str(root), *arguments]) == 0
    capsys.readouterr()
    (root / "widget.py").write_text("value = 3\n", encoding="utf-8")
    (root / "plan.json").write_text(json.dumps({"grade": "widget-check", "inputs": ["widget.py"],
        "environment": {"target": "fictional"}, "commands": [{"argv": [python_binary(), "-c", "pass"]}]}), encoding="utf-8")
    (root / "scope.json").write_text('["widget.py"]', encoding="utf-8")
    (root / "findings.json").write_text('[]', encoding="utf-8")
    folder = root / "state/widget"
    effective_adapter = configuration.read_adapter(configuration.locate(str(root), "claude").adapter_path, "claude")
    with execution.workspace_scope(root):
        yield root, folder, execution.registry_directory(), effective_adapter


def request_file(sample, script="print('widget inspected')", *, timeout=10, limit=1024):
    root, folder, _, adapter = sample
    (root / "reviewer.py").write_text(script + "\n", encoding="utf-8")
    plan = plans.load_plan(root, root / "plan.json", adapter["verify"]["grades"])
    row = read_table(folder / "units.tsv", UNIT_HEADER)[0]
    plan["unitBrief"] = {name: row[name] for name in EDITABLE_FIELDS}
    argv = [python_binary(), "reviewer.py"]
    command = {"argv": argv, "cwd": "."}
    before = plans.snapshot(root, plan["inputs"])
    runtime = review_dispatch.runtime_identity(root, command, lambda command: None)
    plan_source = references.file_pointer(root, "plan.json")
    implementation = implementation_digest()
    identity = digest({"project": str(root.resolve()), "plan": plan, "settings": adapter["verify"],
        "snapshot": before, "command": command, "runtime": runtime, "references": {},
        "environment": review_dispatch.effective_environment(root, plan), "timeout": timeout, "planSource": plan_source,
        "implementation": implementation, "maxLogBytes": limit, "adapter": adapter, "runtimeName": "claude"})
    job = execution.reserve_review(folder, "U1", identity, "widget inspection", ["widget.py"], [])
    directory = folder / "verification/review-jobs" / job["key"] / job["token"]
    directory.mkdir(parents=True)
    path = directory / "request.json"
    request = {"schema": 1, "project": str(root), "folder": str(folder), "unit": "U1", "plan": plan,
        "settings": adapter["verify"], "adapter": adapter, "runtimeName": "claude", "command": command,
        "argv": argv, "timeout": timeout, "before": before, "runtime": runtime, "references": {},
        "requestIdentity": identity, "job": job, "maxLogBytes": limit, "planSource": plan_source,
        "implementation": implementation}
    path.write_text(json.dumps(request), encoding="utf-8")
    return path, request


def current_job(sample, request):
    return execution.read_record(execution.job_path(execution.job_directory(sample[1]), request["job"]["key"]))


def launch_worker(path):
    return subprocess.Popen([python_binary(), str(ENTRY), "_review-worker", "--input", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def cli_arguments(root, timeout=10):
    return [python_binary(), str(ENTRY), "--project", str(root), "review-run", "U1", "--plan", "plan.json",
        "--snapshot", digest(plans.snapshot(root, ["widget.py"])), "--purpose", "widget inspection",
        "--scope", "scope.json", "--findings", "findings.json", "--timeout-seconds", str(timeout),
        "--", python_binary(), "reviewer.py"]


HOLD = """import os, time
from pathlib import Path
Path('child-ready').write_text(str(os.getpid()))
while not Path('release').exists():
    time.sleep(0.02)
print('widget inspected')
"""


@pytest.mark.parametrize("script,timeout,exit_code,timed_out", [
    ("print('widget inspected')", 10, 0, False),
    ("raise SystemExit(7)", 10, 7, False),
    ("import os,time; from pathlib import Path; Path('timed-child').write_text(str(os.getpid())); time.sleep(30)", 1, None, True),
])
def test_worker_normal_failure_and_timeout_release_child_and_inputs(sample, script, timeout, exit_code, timed_out):
    path, request = request_file(sample, script, timeout=timeout)
    with launch_worker(path) as worker:
        out, err = worker.communicate(timeout=15)
        assert worker.returncode == 0, (out, err)
    job = current_job(sample, request)
    assert job["status"] == "completed" and job.get("child") is None and job.get("launchPending") is False
    result = review_dispatch.read_completed(sample[1], job, request["requestIdentity"])
    assert result["exitCode"] == exit_code and result["timedOut"] is timed_out
    assert result["status"] == ("completed" if exit_code == 0 else "failed")
    if timed_out:
        assert not execution.locking.process_alive(int((sample[0] / "timed-child").read_text()))
    execution.require_mutation_allowed([sample[0] / "widget.py"])
    assert json.loads((sample[2] / "inputs.json").read_text())["leases"] == []
    with execution.unit_execution_lock(sample[1], "U1"):
        pass


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(schema=True),
    lambda value: value.update(maxLogBytes=2 ** 40),
    lambda value: value.update(timeout=0),
    lambda value: value.update(argv=["changed-command"]),
    lambda value: value.update(requestIdentity="0" * 64),
    lambda value: value["job"].update(token="../invalid"),
])
def test_malformed_request_does_not_claim_job_or_launch(sample, mutate):
    path, request = request_file(sample, "from pathlib import Path; Path('unexpected').touch()")
    original = current_job(sample, request)
    mutate(request)
    path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(VerificationError):
        review_worker.run_from_file(path)
    assert execution.read_record(execution.job_path(execution.job_directory(sample[1]), original["key"])) == original
    assert not (sample[0] / "unexpected").exists()


def test_unknown_fresh_token_cannot_claim_existing_job(sample):
    path, request = request_file(sample, "from pathlib import Path; Path('unexpected').touch()")
    original = current_job(sample, request)
    request["job"]["token"] = "f" * 32 if original["token"] != "f" * 32 else "e" * 32
    fresh = path.parent.parent / request["job"]["token"] / "request.json"
    fresh.parent.mkdir()
    fresh.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(VerificationError, match="no longer owned"):
        review_worker.run_from_file(fresh)
    assert current_job(sample, request) == original
    assert not (sample[0] / "unexpected").exists()


@pytest.mark.parametrize("raw", [b"[", b"[]", b'{"nested":' * 1500 + b'0' + b'}' * 1500,
                               b"x" * (review_worker.MAX_REQUEST_BYTES + 1)],
                         ids=["malformed", "array", "deeply-nested", "oversized"])
def test_unreadable_or_unbounded_request_does_not_transfer_ownership(sample, raw):
    path, request = request_file(sample)
    original = current_job(sample, request)
    path.write_bytes(raw)
    with pytest.raises(VerificationError, match="invalid review supervisor request"):
        review_worker.run_from_file(path)
    assert current_job(sample, request) == original


def test_forged_command_and_recomputed_digest_cannot_reuse_registered_token(sample):
    path, request = request_file(sample)
    original = current_job(sample, request)
    request["argv"] = [python_binary(), "-c", "raise SystemExit(99)"]
    request["command"] = {"argv": request["argv"], "cwd": "."}
    request["runtime"] = review_dispatch.runtime_identity(sample[0], request["command"], lambda command: None)
    request["requestIdentity"] = digest({"project": request["project"], "plan": request["plan"],
        "settings": request["settings"], "snapshot": request["before"], "command": request["command"],
        "runtime": request["runtime"], "references": request["references"],
        "environment": review_dispatch.effective_environment(sample[0], request["plan"]), "timeout": request["timeout"],
        "planSource": request["planSource"], "implementation": request["implementation"],
        "maxLogBytes": request["maxLogBytes"], "adapter": request["adapter"], "runtimeName": request["runtimeName"]})
    path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(VerificationError, match="no longer owned"):
        review_worker.run_from_file(path)
    assert current_job(sample, request) == original


def test_valid_request_cannot_be_claimed_by_a_second_worker(sample):
    root, _, _, _ = sample
    path, request = request_file(sample, HOLD)
    with launch_worker(path) as first:
        try:
            eventually(lambda: (root / "child-ready").exists())
            owner = current_job(sample, request)
            with launch_worker(path) as second:
                second.communicate(timeout=8)
                assert second.returncode != 0
            assert current_job(sample, request) == owner
            assert first.poll() is None
        finally:
            (root / "release").touch()
            out, err = first.communicate(timeout=10)
        assert first.returncode == 0, (out, err)


@pytest.mark.parametrize("target", ["plan.json", "widget.py", "reviewer.py", "units.tsv"])
def test_changed_pinned_inputs_prevent_actual_launch(sample, target):
    path, request = request_file(sample, "from pathlib import Path; Path('unexpected').touch()")
    root, folder, _, _ = sample
    source = folder / target if target == "units.tsv" else root / target
    text = source.read_text(encoding="utf-8")
    source.write_text(text.replace("Inspect widget", "Inspect changed widget") if target == "units.tsv" else text + " ", encoding="utf-8")
    with launch_worker(path) as worker:
        worker.communicate(timeout=12)
        assert worker.returncode != 0
    assert not (root / "unexpected").exists()
    assert current_job(sample, request) is None


def test_logs_are_capped_while_both_streams_are_still_live(sample):
    root, folder, _, _ = sample
    path, request = request_file(sample, "import os\nos.write(1,b'a'*250000)\nos.write(2,b'b'*230000)\n" + HOLD, limit=128)
    directory = path.parent
    with launch_worker(path) as worker:
        try:
            eventually(lambda: (root / "child-ready").exists())
            assert worker.poll() is None
            eventually(lambda: (directory / "stdout.log").stat().st_size == 128)
            assert (directory / "stderr.log").stat().st_size == 128
            assert current_job(sample, request)["child"]["pid"] == int((root / "child-ready").read_text())
            with pytest.raises(VerificationError, match="protected execution input"):
                execution.require_mutation_allowed([root / "widget.py"])
        finally:
            (root / "release").touch()
            out, err = worker.communicate(timeout=12)
        assert worker.returncode == 0, (out, err)
    result = review_dispatch.read_completed(folder, current_job(sample, request), request["requestIdentity"])
    assert all(result[name]["truncated"] and result[name]["bytes"] == 128 for name in ("stdout", "stderr"))


def test_cli_death_leaves_worker_and_input_ownership_live(sample):
    root, folder, _, _ = sample
    (root / "reviewer.py").write_text(HOLD, encoding="utf-8")
    cli = subprocess.Popen(cli_arguments(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        eventually(lambda: (root / "child-ready").exists())
        path = next(execution.job_directory(folder).glob("*.json"))
        job = execution.read_record(path)
        assert job["pid"] != cli.pid and execution.process_owner_alive(job)
        cli.kill()
        cli.communicate(timeout=5)
        duplicate = subprocess.run(cli_arguments(root), capture_output=True, timeout=8)
        assert duplicate.returncode == 0, duplicate.stderr
        output = json.loads(duplicate.stdout)
        assert output["status"] == "running" and not output["dispatched"]
        assert output["job"]["token"] == job["token"]
        with pytest.raises(VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "widget.py"])
        with pytest.raises(execution.ExecutionBusy):
            with execution.unit_execution_lock(folder, "U1"):
                pytest.fail("unit lock acquired during live review")
    finally:
        (root / "release").touch()
        if cli.poll() is None:
            cli.kill()
            cli.communicate(timeout=5)
    eventually(lambda: execution.read_record(path).get("status") == "completed")
    # Completion is published just before the supervisor exits input_lease().
    # Wait for that separate cleanup checkpoint before asserting write access.
    eventually(lambda: not execution.has_input_leases())
    execution.require_mutation_allowed([root / "widget.py"])


def test_worker_death_keeps_registered_live_child_protected(sample):
    root, folder, _, _ = sample
    path, request = request_file(sample, HOLD)
    worker = launch_worker(path)
    try:
        eventually(lambda: (root / "child-ready").exists())
        child_pid = int((root / "child-ready").read_text())
        worker.kill()
        worker.communicate(timeout=5)
        stored = current_job(sample, request)
        assert stored["child"]["pid"] == child_pid and execution.child_alive(stored)
        duplicate = execution.reserve_review(folder, "U1", request["requestIdentity"], "widget inspection", ["widget.py"], [])
        assert not duplicate["acquired"] and duplicate["token"] == request["job"]["token"]
        with pytest.raises(VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "widget.py"])
        with pytest.raises(execution.ExecutionBusy):
            with execution.unit_execution_lock(folder, "U1"):
                pytest.fail("unit lock acquired with surviving child")
    finally:
        (root / "release").touch()
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=5)


def test_worker_death_between_spawn_and_registration_requires_recovery(sample):
    root, folder, _, _ = sample
    path, request = request_file(sample, HOLD)
    script = "from chef.verification import execution; from chef.mission import review_worker; import os,sys; " \
        "execution.register_job_child=lambda *a: os._exit(77); review_worker.run_from_file(sys.argv[1])"
    with subprocess.Popen([python_binary(), "-c", script, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
        try:
            worker.communicate(timeout=8)
            assert worker.returncode == 77
            eventually(lambda: (root / "child-ready").exists())
            stored = current_job(sample, request)
            assert stored["launchPending"] is True and stored["child"] is None
            duplicate = execution.reserve_review(folder, "U1", request["requestIdentity"], "widget inspection", ["widget.py"], [])
            assert not duplicate["acquired"]
            with pytest.raises(VerificationError, match="protected execution input"):
                execution.require_mutation_allowed([root / "widget.py"])
        finally:
            (root / "release").touch()


def test_bounded_process_reports_seen_and_retained_bytes(sample):
    root, folder, _, _ = sample
    _, request = request_file(sample)
    job = execution.transfer_job(folder, request["job"]["key"], request["job"]["token"], os.getpid())
    with execution.input_lease([root / "widget.py"]) as lease:
        code, timed_out, counts = review_worker.bounded_process(
            [python_binary(), "-c", "import os; os.write(1,b'a'*170000); os.write(2,b'b'*190000)"],
            root, root / "stdout", root / "stderr", 5, 91, folder=folder, job=job, lease=lease)
        assert code == 0 and timed_out is False
        assert counts == [{"seen": 170000, "written": 91}, {"seen": 190000, "written": 91}]
    execution.finish_job(folder, job["key"], job["token"], release=True)


@pytest.mark.parametrize("key", ["PATH", "PWD"])
def test_declared_environment_identifies_the_actual_reviewer_child_environment(sample, monkeypatch, key):
    root, folder, _, _ = sample
    plan_path = root / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["environmentKeys"] = [key]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    (root / "reviewer.py").write_text(
        "import json,os\nprint(json.dumps({" + repr(key) + ":os.environ.get(" + repr(key) + ")}))\n", encoding="utf-8")
    # Include the machine's ordinary child-only bins before adding a fresh,
    # existing fixture bin in the real caller subprocess. The new bin must be
    # added once by child_env and remain identical in worker/reviewer children.
    monkeypatch.setenv("PATH", infrastructure_process.child_env(str(root))["PATH"])
    extra_bin = root / "fixture-bin"
    extra_bin.mkdir()
    monkeypatch.setenv("PWD", str(root.parent / "different-working-directory"))
    launcher = "import os,sys; from chef.infrastructure import process; " \
        "from chef.mission import state; process.P4_INSTALLATIONS=[os.path.join(sys.argv[1],'p4')]; " \
        "raise SystemExit(state.main(sys.argv[2:]))"
    command = [python_binary(), "-c", launcher, str(extra_bin), *cli_arguments(root)[2:]]
    completed = subprocess.run(command, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)["result"]
    stdout_path = references.resolve_path(folder, result["stdout"]["file"]["path"])
    observed = json.loads(stdout_path.read_text(encoding="utf-8"))[key]
    expected = os.pathsep.join([str(extra_bin), os.environ["PATH"]]) if key == "PATH" else str(root)
    assert observed == expected
    assert result["status"] == "completed" and result["exitCode"] == 0
    reused = subprocess.run(command, capture_output=True, timeout=15)
    assert reused.returncode == 0, reused.stderr
    reused_output = json.loads(reused.stdout)
    assert reused_output["dispatched"] is False and reused_output["result"] == result


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup requires POSIX")
def test_timeout_stops_owned_process_group_including_pipe_holding_child(sample):
    descendant = "import time; from pathlib import Path; time.sleep(3); Path('escaped-timeout').touch()"
    script = "import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c'," \
        + repr(descendant) + "])\nPath('descendant-ready').write_text(str(p.pid))\ntime.sleep(30)"
    path, request = request_file(sample, script, timeout=1)
    with launch_worker(path) as worker:
        out, err = worker.communicate(timeout=12)
        assert worker.returncode == 0, (out, err)
    assert (sample[0] / "descendant-ready").exists()
    child_pid = int((sample[0] / "descendant-ready").read_text())
    if sys.platform.startswith("linux"):
        status = Path(f"/proc/{child_pid}/stat")
        assert not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    else:
        inspected = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, timeout=5)
        assert inspected.returncode != 0 or not inspected.stdout.strip() or inspected.stdout.strip().startswith(b"Z")
    result = review_dispatch.read_completed(sample[1], current_job(sample, request), request["requestIdentity"])
    assert result["timedOut"] is True
    assert not (sample[0] / "escaped-timeout").exists()
