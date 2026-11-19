import copy
import json
import sys
from pathlib import Path

import pytest
from chef.mission import supervisor as sup

from tests.paths import PLUGIN_ROOT


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / "fictional-project"
    project.mkdir()
    config = {"schema": 1, "projects": {"demo": {"path": str(project), "slots": ["one", "two"],
              "runtimes": ["codex", "grok"], "resources": []}},
              "budgets": {"maxRuns": 4, "maxConcurrent": 2, "maxSeconds": 100, "launchTimeoutSeconds": 10}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("CHEF_SUPERVISOR_HOME", str(tmp_path / "supervisor"))
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "unused-registry"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    state_path = tmp_path / "supervisor" / "queue.json"
    state = sup.load_state(state_path, config, now=100)

    def observer(registered, slot, runtime, mission=""):
        return {"path": str(tmp_path / "slots" / slot), "ready": True, "active": "", "session": "",
                "corrupt": False, "missions": [], "completed": False, "interrupted": False}
    return config, state, observer, config_path, state_path


def add(state, config, task_id="first", **kwargs):
    return sup.enqueue(state, config, task_id, "demo", "Build fictional counter", **kwargs)


def test_priority_dependencies_and_cycle_validation(setup):
    config, state, observer, *_ = setup
    add(state, config)
    add(state, config, "dependent", priority=50, after=["first"])
    add(state, config, "urgent", priority=10)
    result = sup.plan(state, config, observer, lambda _: True, now=101)
    assert [item["id"] for item in result["dispatch"]] == ["urgent", "first"]
    assert result["blocked"]["dependent"] == "dependencies incomplete"
    state["tasks"][0]["after"] = ["dependent"]
    with pytest.raises(sup.SupervisorError, match="cycle"):
        sup.validate_state(state, config)


@pytest.mark.parametrize("kwargs", [{"after": ["missing"]}, {"runtime": "claude"}, {"after": ["first"]}])
def test_enqueue_rejects_invalid_contract_without_partial_mutation(setup, kwargs):
    config, state, *_ = setup
    with pytest.raises(sup.SupervisorError):
        add(state, config, **kwargs)
    assert state["tasks"] == []


@pytest.mark.parametrize("goal", ["", "bad\nline", 'bad"quote', "bad\x00null"])
def test_goals_cannot_break_slot_task_contract(setup, goal):
    config, state, *_ = setup
    with pytest.raises(sup.SupervisorError):
        sup.enqueue(state, config, "first", "demo", goal)


@pytest.mark.parametrize("change", ["busy", "corrupt", "unready", "unavailable"])
def test_plan_fails_closed_without_launch_or_mutation(setup, change):
    config, state, observer, *_ = setup
    add(state, config)
    before = copy.deepcopy(state)
    def changed(*args):
        view = observer(*args)
        view.update(active="other" if change == "busy" else "", corrupt=change == "corrupt",
                    ready=change != "unready")
        return view
    result = sup.dispatch(state, config, lambda _: pytest.fail("saved"), observer=changed,
                          readiness=lambda _: change != "unavailable", now=101,
                          launcher=lambda *_: pytest.fail("launched"))
    assert not result["dispatch"]
    assert state == before


def test_durable_reservation_precedes_launcher_and_holds_both_slots(setup):
    config, state, observer, _, state_path = setup
    add(state, config)
    add(state, config, "second")
    launched = []
    def launcher(task, project):
        disk = json.loads(state_path.read_text())
        saved = next(t for t in disk["tasks"] if t["id"] == task["id"])
        assert saved["status"] == "reserved"
        assert disk["runs"] == len(launched) + 1
        launched.append(task["assignment"]["slot"])
        return 0
    sup.dispatch(state, config, lambda value: sup.atomic_write(state_path, value), True,
                 observer, lambda _: True, launcher, now=101)
    assert launched == ["one", "two"]
    assert all(task["status"] == "launched" for task in state["tasks"])
    add(state, config, "third")
    assert not sup.plan(state, config, observer, lambda _: True, now=102)["dispatch"]


@pytest.mark.parametrize("collision", ["resource", "alias", "nested"])
def test_resource_and_canonical_path_collisions(setup, collision):
    config, state, observer, *_ = setup
    if collision == "resource":
        config["projects"]["demo"]["resources"] = ["shared-editor"]
        state["configHash"] = sup.digest(config)
    add(state, config)
    add(state, config, "second")
    def changed(*args):
        view = observer(*args)
        if collision != "resource":
            view["path"] = str(Path(view["path"]).parent / "one")
            if collision == "nested" and args[1] == "two":
                view["path"] += "/nested"
        return view
    assert len(sup.plan(state, config, changed, lambda _: True, now=101)["dispatch"]) == 1


@pytest.mark.parametrize("key,value", [("maxRuns", 1), ("maxConcurrent", 1), ("maxSeconds", 1)])
def test_budgets_bound_launches(setup, key, value):
    config, state, observer, *_ = setup
    config["budgets"][key] = value
    state["configHash"] = sup.digest(config)
    add(state, config)
    add(state, config, "second")
    planned = sup.plan(state, config, observer, lambda _: True, now=102)["dispatch"]
    assert len(planned) == (0 if key == "maxSeconds" else 1)


def test_dispatch_rechecks_time_after_each_launch(setup, monkeypatch):
    config, state, observer, *_ = setup
    add(state, config)
    add(state, config, "second")
    current = [101]
    monkeypatch.setattr(sup.time, "time", lambda: current[0])
    launched = []
    def launcher(task, project):
        launched.append(task["id"])
        current[0] = 300
        return 0
    sup.dispatch(state, config, lambda _: None, True, observer, lambda _: True, launcher)
    assert launched == ["first"]


@pytest.mark.parametrize("failure", ["exit", "timeout", "death"])
def test_failed_or_interrupted_launch_never_requeues(setup, failure):
    config, state, observer, _, state_path = setup
    add(state, config)
    def launcher(*_):
        if failure == "timeout":
            raise TimeoutError("fictional timeout")
        if failure == "death":
            raise KeyboardInterrupt()
        return 1
    if failure == "death":
        with pytest.raises(KeyboardInterrupt):
            sup.dispatch(state, config, lambda value: sup.atomic_write(state_path, value), True,
                         observer, lambda _: True, launcher, now=101)
        state = sup.load_state(state_path, config)
        assert state["tasks"][0]["status"] == "reserved"
        sup.reconcile(state, config, observer, now=102)
    else:
        sup.dispatch(state, config, lambda _: None, True, observer, lambda _: True, launcher, now=101)
    assert state["tasks"][0]["status"] == "needs-operator"
    assert state["runs"] == 1
    assert state["tasks"][0]["assignment"]


def test_reconcile_requires_observed_active_then_validated_completion(setup):
    config, state, observer, *_ = setup
    task = add(state, config)
    sup.dispatch(state, config, lambda _: None, True, observer, lambda _: True, lambda *_: 0, now=101)
    sup.reconcile(state, config, observer, now=102)
    assert task["status"] == "launched"
    def running(*args):
        return {**observer(*args), "active": "20260101-fictional", "session": "fictional-session"}
    sup.reconcile(state, config, running, now=103)
    assert task["status"] == "running"
    def completed(*args):
        assert args[3] == "20260101-fictional"
        return {**observer(*args), "completed": True}
    sup.reconcile(state, config, completed, now=104)
    assert task["status"] == "completed"


@pytest.mark.parametrize("case", ["timeout", "old", "session", "missing", "corrupt", "stale", "interrupted"])
def test_ambiguous_ownership_is_operator_candidate(setup, case):
    config, state, observer, *_ = setup
    task = add(state, config)
    sup.dispatch(state, config, lambda _: None, True, observer, lambda _: True, lambda *_: 0, now=101)
    if case == "old":
        task["assignment"]["knownMissions"] = ["old-mission"]
    if case in ("session", "missing"):
        task.update(status="running", mission="old-mission", session="original")
    def changed(*args):
        return {**observer(*args), "active": "old-mission" if case in ("old", "session") else "",
                "session": "different", "corrupt": case == "corrupt", "stale": case == "stale",
                "interrupted": case == "interrupted"}
    sup.reconcile(state, config, changed, now=120)
    assert task["status"] == "needs-operator"
    assert task["assignment"]


def test_launcher_uses_only_existing_slot_run_and_literal_goal(setup, monkeypatch):
    config, state, observer, *_ = setup
    task = add(state, config)
    task["assignment"] = {"slot": "one"}
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(sup.subprocess, "run", run)
    assert sup.launch(task, config["projects"]["demo"]) == 0
    argv, options = calls[0]
    assert argv == [sys.executable, str(sup.SLOTS_SCRIPT), "--project", config["projects"]["demo"]["path"],
                    "--runtime", "codex", "run", "one", "--", task["goal"]]
    assert options["shell"] is False
    assert "--resume" not in argv and "--editor" not in argv


def test_cli_status_and_plan_do_not_create_files_and_config_drift_fails(setup, capsys):
    config, state, _, config_path, state_path = setup
    args = ["--config", str(config_path), "--state", str(state_path)]
    assert sup.main([*args, "status"]) == 0
    assert not state_path.exists() and not Path(str(state_path) + ".lock").exists()
    assert sup.main([*args, "enqueue", "first", "--project", "demo", "--goal", "Fictional work"]) == 0
    disk = sup.load_state(state_path, config)
    assert disk["tasks"][0]["status"] == "pending"
    config["budgets"]["maxRuns"] += 1
    with pytest.raises(sup.SupervisorError, match="mismatch"):
        sup.load_state(state_path, config)


def test_cli_refuses_concurrent_queue_mutation(setup):
    _, _, _, config_path, state_path = setup
    with sup.slots.FileLock(str(state_path) + ".lock", "test"):
        assert sup.main(["--config", str(config_path), "--state", str(state_path), "enqueue", "first",
                         "--project", "demo", "--goal", "Fictional work"]) == 1
    assert not state_path.exists()


@pytest.fixture
def real_slot(setup):
    config, state, _, _, _ = setup
    project = config["projects"]["demo"]
    root = Path(project["path"])
    adapter_dir = root / ".claude" / "orchestrator"
    adapter_dir.mkdir(parents=True)
    slot_root = root.parent / "slots"
    adapter = {"schema": 1, "project": "Fictional", "paths": {"stateDir": "records"},
               "verify": {"grades": ["pytest-pass"]},
               "slots": {"enabled": True, "root": str(slot_root), "names": ["one", "two"],
                         "client": "fictional_{name}", "prepare": [], "vcs": "none"}}
    (adapter_dir / "adapter.json").write_text(json.dumps(adapter))
    assert sup.slots.main(["--project", str(root), "create", "one"]) == 0
    return config, state, project, root, slot_root, adapter


def test_real_slot_observer_uses_existing_readiness_active_and_evidence(real_slot):
    _, _, project, root, slot_root, _ = real_slot
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    view = sup.observe(project, "one", "codex")
    assert view["ready"] and not view["active"]
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    records = slot_root / "one" / "records"
    records.mkdir()
    (records / "ACTIVE").write_text(json.dumps({"id": "20260101-fictional", "session": "fictional-session"}))
    view = sup.observe(project, "one", "codex")
    assert view["active"] == "20260101-fictional" and not view["ready"]
    (records / "ACTIVE").unlink()
    mission = records / "20260101-fictional"
    mission.mkdir()
    (mission / "mission.md").write_text("상태: 닫힘\n확인 C: 승인\n## 목표\nFictional work\n", encoding="utf-8")
    unit = {key: "fictional" for key in sup.mission_state.UNIT_HEADER}
    unit.update(id="M1", status="done")
    sup.mission_state.write_table(mission / "units.tsv", sup.mission_state.UNIT_HEADER, [unit])
    # A closed header and done TSV cannot stand in for the actual receipt/review.
    with pytest.raises(sup.verification.VerificationError):
        sup.observe(project, "one", "codex", "20260101-fictional")


@pytest.mark.parametrize("mutation", [None, "source", "review-evidence"])
def test_real_receipt_review_completion_and_dependency_gates(real_slot, mutation):
    config, state, project, _, slot_root, adapter = real_slot
    task = add(state, config)
    add(state, config, "dependent", after=[task["id"]])
    sup.dispatch(state, config, lambda _: None, True, sup.observe, lambda _: True,
                 lambda *_: 0, now=101)
    assert task["status"] == "launched"
    assert sup.plan(state, config, sup.observe, lambda _: True, now=102)["blocked"]["dependent"] == "dependencies incomplete"

    workspace = slot_root / "one"
    records = workspace / "records"
    mission = records / "20260101-fictional"
    mission.mkdir(parents=True)
    active = records / "ACTIVE"
    active.write_text(json.dumps({"id": mission.name, "session": "fictional-session"}))
    mission_document = mission / "mission.md"
    mission_document.write_text("상태: 진행\n확인 C: 대기\n## 목표\nFictional work\n", encoding="utf-8")
    sup.reconcile(state, config, sup.observe, now=102)
    assert task["status"] == "running" and task["mission"] == mission.name

    source = workspace / "src.py"
    source.write_text("value = 1\n")
    plan = {"grade": "pytest-pass", "inputs": ["src.py"], "environment": {"target": "fictional"},
            "commands": [{"argv": [sys.executable, "-c",
                          "import runpy; assert runpy.run_path('src.py')['value'] == 1"], "timeoutSeconds": 10}]}
    receipt_path, receipt, reused = sup.verification.run(
        workspace, mission, "M1", plan, lambda _: None, settings=adapter["verify"])
    assert receipt["status"] == "pass" and not reused
    evidence = mission / "independent-review.md"
    evidence.write_text("Synthetic independent review fixture: no findings.\n")
    review_path = sup.verification.record_review(
        workspace, mission, "M1", receipt_path, evidence, "fictional-independent-reviewer", "unit", "clean",
        settings=adapter["verify"])
    assert Path(review_path).is_file()

    # These approval/done records are synthetic fixtures only, never production approvals.
    unit = {key: "fictional" for key in sup.mission_state.UNIT_HEADER}
    unit.update(id="M1", status="done")
    sup.mission_state.write_table(mission / "units.tsv", sup.mission_state.UNIT_HEADER, [unit])
    mission_document.write_text("상태: 닫힘\n확인 C: 승인\n## 목표\nFictional work\n", encoding="utf-8")
    active.unlink()
    if mutation == "source":
        source.write_text("value = 2\n")
    elif mutation == "review-evidence":
        evidence.write_text("Synthetic changed review evidence.\n")

    sup.reconcile(state, config, sup.observe, now=103)
    if mutation is None:
        assert task["status"] == "completed"
        selected = sup.plan(state, config, sup.observe, lambda _: True, now=104)["dispatch"]
        assert [item["id"] for item in selected] == ["dependent"]
    else:
        assert task["status"] == "needs-operator"
        assert task["assignment"]
        result = sup.plan(state, config, sup.observe, lambda _: True, now=104)
        assert not result["dispatch"] and result["blocked"]["dependent"] == "dependencies incomplete"


def test_runtime_readiness_requires_both_terminal_and_runtime(monkeypatch):
    monkeypatch.setattr(sup.shutil, "which", lambda name: "/fictional/runtime" if name == "codex" else None)
    assert not sup.runtime_ready("codex")
    monkeypatch.setattr(sup.shutil, "which", lambda name: "/fictional/terminal" if name == "wt.exe" else None)
    assert not sup.runtime_ready("codex")


@pytest.mark.parametrize("command", [["status"], ["plan"], ["dispatch", "--execute"], ["reconcile"],
                                     ["enqueue", "second", "--project", "demo", "--goal", "Fictional work"]])
def test_other_queue_paths_cannot_bypass_shared_authority(setup, command, tmp_path):
    config, state, observer, config_path, state_path = setup
    add(state, config)
    sup.dispatch(state, config, lambda value: sup.atomic_write(state_path, value), True,
                 observer, lambda _: True, lambda *_: 0, now=101)
    saved = state_path.read_bytes()
    alternate = tmp_path / "other-authority" / "queue.json"
    assert sup.main(["--config", str(config_path), "--state", str(alternate), *command]) == 1
    assert not alternate.parent.exists()
    assert state_path.read_bytes() == saved
    assert sup.load_state(state_path, config)["tasks"][0]["status"] == "launched"


def test_authority_path_default_and_relative_home_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("CHEF_SUPERVISOR_HOME", raising=False)
    monkeypatch.setattr(sup.Path, "home", classmethod(lambda cls: tmp_path))
    assert sup.authority_path() == tmp_path / ".chef" / "supervisor" / "queue.json"
    assert not (tmp_path / ".chef").exists()
    monkeypatch.setenv("CHEF_SUPERVISOR_HOME", "relative")
    with pytest.raises(sup.SupervisorError, match="absolute"):
        sup.authority_path()


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), -1])
def test_corrupt_queue_clocks_cannot_disable_time_budget(setup, value):
    config, state, observer, *_ = setup
    state["createdAt"] = value
    with pytest.raises(sup.SupervisorError):
        sup.validate_state(state, config)
    state["createdAt"] = 100
    task = add(state, config)
    sup.dispatch(state, config, lambda _: None, True, observer, lambda _: True, lambda *_: 0, now=101)
    task["reservedAt"] = value
    with pytest.raises(sup.SupervisorError):
        sup.validate_state(state, config)


def test_corrupt_runs_and_mission_paths_are_rejected(setup):
    config, state, observer, *_ = setup
    task = add(state, config)
    sup.dispatch(state, config, lambda _: None, True, observer, lambda _: True, lambda *_: 0, now=101)
    state["runs"] = 0
    with pytest.raises(sup.SupervisorError, match="count"):
        sup.validate_state(state, config)
    state["runs"] = 1
    task["mission"] = "../../outside"
    with pytest.raises(sup.SupervisorError, match="mission"):
        sup.validate_state(state, config)


@pytest.mark.parametrize("schema", [True, False, 1.0])
def test_schema_requires_actual_integer_in_config_and_queue(setup, schema):
    config, state, _, config_path, _ = setup
    invalid_config = {**config, "schema": schema}
    config_path.write_text(json.dumps(invalid_config))
    with pytest.raises(sup.SupervisorError, match="schema"):
        sup.load_config(config_path)
    state["schema"] = schema
    with pytest.raises(sup.SupervisorError, match="schema"):
        sup.validate_state(state, config)
