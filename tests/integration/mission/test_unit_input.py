"""Batch unit registration must not leave a partially accepted plan."""

import json
from datetime import datetime, timedelta

import pytest

from chef.mission import state
from chef.mission.storage import read_table, write_table
from chef.mission.contracts import UNIT_HEADER
from tests.integration.mission.test_state_efficiency import sample


def unit(name, **changes):
    return {"id": name, "goal": "Check a fictional widget", "files": "widget.py",
            "done": "Expected value", "verify": "sample checks", "timebox": 30,
            "forbid": "publish", **changes}


def save(root, items):
    (root / "units.json").write_text(json.dumps({"schema": 1, "units": items}), encoding="utf-8")


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_template_is_standalone_and_writes_nothing(tmp_path, monkeypatch, capsys, runtime):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(state.infrastructure_configuration, "locate", lambda *a: pytest.fail("adapter lookup"))
    assert state.main(["unit", "template", "--id", "U2", "--id", "U3"], runtime=runtime) == 0
    data = json.loads(capsys.readouterr().out)
    assert [u["id"] for u in data["units"]] == ["U2", "U3"]
    assert data["units"][0]["timebox"] == 30
    assert data["units"][0]["forbid"] == ""
    assert list(tmp_path.iterdir()) == []


def test_all_validation_errors_return_before_any_write(sample):
    root, folder, run = sample
    save(root, [unit("U2", forbid="", timebox="30 minutes", after="absent"),
                unit("U1", done="", extra="unexpected")])
    before = (folder / "units.tsv").read_bytes()
    code, _, err = run("unit", "add", "--input", "units.json")
    assert code == 1
    for expected in ("forbid", "양의 정수", "absent", "이미 있음", "done", "extra"):
        assert expected in err
    assert (folder / "units.tsv").read_bytes() == before


def test_batch_is_one_atomic_write_and_dependencies_use_input_order(sample, monkeypatch):
    root, folder, run = sample
    save(root, [unit("U2", after="U1"), unit("U3", after="U2")])
    original = state.write_table
    calls = []

    def write(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(state, "write_table", write)
    code, out, err = run("unit", "add", "--input", "units.json")
    assert code == 0 and not err and len(calls) == 1
    response = json.loads(out)
    assert response["status"] == "added"
    assert [(u["id"], u["after"], u["status"]) for u in response["units"]] == [
        ("U2", "U1", "todo"), ("U3", "U2", "todo")]
    assert len(read_table(str(folder / "units.tsv"), UNIT_HEADER)) == 3


def test_dry_run_leaves_all_mission_files_unchanged(sample):
    root, folder, run = sample
    save(root, [unit("U2")])
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.parent.rglob("*") if p.is_file()}
    code, out, err = run("unit", "add", "--input", "units.json", "--dry-run")
    assert code == 0 and not err and json.loads(out)["status"] == "valid"
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.parent.rglob("*") if p.is_file()}


@pytest.mark.parametrize("items", [
    [unit("U2"), unit("U2")],
    [unit("U2", after="U3"), unit("U3", after="U2")],
    [unit("U2", timebox=True)],
    [unit("U2", files=["widget.py"])],
])
def test_invalid_batch_does_not_change_state(sample, items):
    root, folder, run = sample
    save(root, items)
    before = (folder / "units.tsv").read_bytes()
    assert run("unit", "add", "--input", "units.json")[0] == 1
    assert (folder / "units.tsv").read_bytes() == before


def test_json_and_flags_cannot_silently_override_each_other(sample):
    root, _, run = sample
    save(root, [unit("U2")])
    assert run("unit", "add", "U3", "--input", "units.json")[0] == 1
    assert run("unit", "add", "--goal", "Other", "--input", "units.json")[0] == 1


def test_resumed_context_keeps_approval_and_unit_pointers(sample):
    root, folder, run = sample
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    code, out, err = run("context", "--json")
    data = json.loads(out)
    assert code == 0 and not err and data["active"] == ["U1"]
    assert data["unitSource"] == str(folder / "units.tsv")
    assert str(folder / "mission.md") in data["approvalSources"]
    assert data["semanticApproval"] == "read_original_sources"
    assert before == {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_overdue_brief_returns_replanning_diagnostics_without_dispatch_or_write(sample):
    root, folder, run = sample
    path = str(folder / "units.tsv")
    rows = read_table(path, UNIT_HEADER)
    rows[0]["started"] = (datetime.now().astimezone() - timedelta(minutes=40)).isoformat()
    write_table(path, UNIT_HEADER, rows)
    before = (folder / "units.tsv").read_bytes()
    code, out, err = run("brief", "U1")
    data = json.loads(out)
    assert code == 1 and not err
    assert data["dispatch"]["status"] == "needs-replan"
    assert data["dispatch"]["elapsedMinutes"] >= 40
    assert data["dispatch"]["timeboxMinutes"] == 30
    assert data["approvalSources"] == [str(folder / "mission.md")]
    assert before == (folder / "units.tsv").read_bytes()
    assert json.loads(run("context", "--json")[1])["overdue"] == ["U1"]
