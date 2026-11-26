import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.guards import mission
from chef.workers import edits


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "assigned.txt").write_text("before", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    owner = {"runtime": "codex", "session": "fixture-owner", "mission": "fixture-mission", "state": str(state),
             "files": ["assigned.txt"]}
    (state / "ACTIVE").write_text(json.dumps({"id": owner["mission"], "session": owner["session"], "runtime": "codex"}))
    data = {"owner": owner, "cwd": str(root), "backend": "grok", "role": "dev-medium", "phase": "work", "guard": None}
    monkeypatch.setattr(mission, "decide", lambda _: SimpleNamespace(deny=""))
    return root, data, tmp_path


def proposal(before, path="assigned.txt", content="after"):
    return json.dumps({"summary": "changed", "changes": [{"path": path, "beforeSha256": before.get(path), "content": content}]})


def test_checked_edit_applies_and_records_journal(workspace):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    assert edits.apply(proposal(before), cwd=root, before=before, data=data, directory=directory) == "changed"
    assert (root / "assigned.txt").read_text() == "after"
    journal = json.loads((directory / "apply-journal.json").read_text())
    assert journal["status"] == "completed" and len(journal["applied"]) == 1


@pytest.mark.parametrize("name", ["other.txt", "../outside.txt", ".git/config"])
def test_unassigned_edit_never_writes(workspace, name):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    with pytest.raises(ValueError, match="outside assigned"):
        edits.apply(proposal(before, name), cwd=root, before=before, data=data, directory=directory)
    assert (root / "assigned.txt").read_text() == "before"


def test_wrong_preimage_is_rejected(workspace):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    value = json.loads(proposal(before))
    value["changes"][0]["beforeSha256"] = "0" * 64
    with pytest.raises(ValueError, match="preimage"):
        edits.apply(json.dumps(value), cwd=root, before=before, data=data, directory=directory)
    assert (root / "assigned.txt").read_text() == "before"


def test_concurrent_edit_is_preserved(workspace):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    (root / "assigned.txt").write_text("other actor")
    with pytest.raises(ValueError, match="preimage"):
        edits.apply(proposal(before), cwd=root, before=before, data=data, directory=directory)
    assert (root / "assigned.txt").read_text() == "other actor"


def test_readonly_checkout_is_not_bypassed(workspace):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    path = root / "assigned.txt"
    path.chmod(0o444)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            edits.apply(proposal(before), cwd=root, before=before, data=data, directory=directory)
        assert path.read_text() == "before"
    finally:
        path.chmod(0o644)


def test_actual_project_guard_can_deny_trusted_apply(workspace, monkeypatch):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    monkeypatch.setattr(mission, "decide", lambda _: SimpleNamespace(deny="fixture denial"))
    with pytest.raises(ValueError, match="fixture denial"):
        edits.apply(proposal(before), cwd=root, before=before, data=data, directory=directory)
    assert (root / "assigned.txt").read_text() == "before"


def test_partial_application_has_durable_evidence(workspace, monkeypatch):
    root, data, directory = workspace
    data["owner"]["files"].append("second.txt")
    before = edits.snapshot(root, data["owner"]["files"])
    value = json.loads(proposal(before))
    value["changes"].append({"path": "second.txt", "beforeSha256": None, "content": "second"})
    original = edits.write_preserving_mode
    def write(path, content, mode):
        if Path(path).name == "second.txt":
            raise OSError("fixture disk failure")
        original(path, content, mode)
    monkeypatch.setattr(edits, "write_preserving_mode", write)
    with pytest.raises(OSError, match="disk failure"):
        edits.apply(json.dumps(value), cwd=root, before=before, data=data, directory=directory)
    journal = json.loads((directory / "apply-journal.json").read_text())
    assert journal["status"] == "prepared" and journal["applied"][0]["path"] == "assigned.txt"


def test_second_guard_denial_prevents_entire_batch(workspace, monkeypatch):
    root, data, directory = workspace
    data["owner"]["files"].append("second.txt")
    before = edits.snapshot(root, data["owner"]["files"])
    value = json.loads(proposal(before))
    value["changes"].append({"path": "second.txt", "beforeSha256": None, "content": "second"})
    monkeypatch.setattr(mission, "decide", lambda payload: SimpleNamespace(
        deny="second denied" if payload["tool_input"]["file_path"].endswith("second.txt") else ""))
    with pytest.raises(ValueError, match="second denied"):
        edits.apply(json.dumps(value), cwd=root, before=before, data=data, directory=directory)
    assert (root / "assigned.txt").read_text() == "before"
    assert not (root / "second.txt").exists()


def test_delete_assigned_file_checks_preimage_and_records_result(workspace):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    edits.apply(proposal(before, content=None), cwd=root, before=before, data=data, directory=directory)
    assert not (root / "assigned.txt").exists()
    journal = json.loads((directory / "apply-journal.json").read_text())
    assert journal["applied"] == [{"path": "assigned.txt", "sha256": None}]


def test_delete_missing_file_is_rejected(workspace):
    root, data, directory = workspace
    data["owner"]["files"] = ["new.txt"]
    before = edits.snapshot(root, ["new.txt"])
    with pytest.raises(ValueError, match="nonexistent"):
        edits.apply(proposal(before, path="new.txt", content=None), cwd=root, before=before, data=data, directory=directory)


def test_cancel_marker_prevents_application(workspace):
    root, data, directory = workspace
    before = edits.snapshot(root, data["owner"]["files"])
    context_file = directory / "context.json"
    context_file.write_text('{"revoked":false}')
    data["contextPath"] = str(context_file)
    (directory / "cancel.json").write_text('{}')
    with pytest.raises(ValueError, match="cancelled"):
        edits.apply(proposal(before), cwd=root, before=before, data=data, directory=directory)
    assert (root / "assigned.txt").read_text() == "before"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_existing_executable_mode_survives_restrictive_umask(workspace):
    root, data, directory = workspace
    path = root / "assigned.txt"
    path.chmod(0o755)
    before = edits.snapshot(root, data["owner"]["files"])
    saved = os.umask(0o077)
    try:
        edits.apply(proposal(before), cwd=root, before=before, data=data, directory=directory)
    finally:
        os.umask(saved)
    assert path.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask")
def test_new_file_respects_restrictive_umask(workspace):
    root, data, directory = workspace
    data["owner"]["files"] = ["new.txt"]
    before = edits.snapshot(root, ["new.txt"])
    saved = os.umask(0o077)
    try:
        edits.apply(proposal(before, path="new.txt"), cwd=root, before=before, data=data, directory=directory)
    finally:
        os.umask(saved)
    assert (root / "new.txt").stat().st_mode & 0o777 == 0o600
