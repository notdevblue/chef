import json
import os
from pathlib import Path

import pytest

from chef.runtimes import hooks
from chef.workers import context


@pytest.fixture
def worker(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    owner = {"runtime": "claude", "session": "fictional-owner", "mission": "fictional-job", "state": str(state),
             "files": ["assigned.txt"]}
    (state / "ACTIVE").write_text(json.dumps({"id": owner["mission"], "session": owner["session"], "runtime": owner["runtime"]}))
    data = {"schema": 1, "pid": os.getpid(), "owner": owner, "backend": "grok", "role": "dev-medium",
            "cwd": str(tmp_path), "digest": "fixture", "phase": "work", "guard": None,
            "ready": str(tmp_path / "ready"), "guardReady": str(tmp_path / "guard-ready")}
    path = tmp_path / "context.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv(context.ENV, str(path))
    monkeypatch.delenv("CHEF_HERMES_WORKER", raising=False)
    monkeypatch.setattr(context, "implementation_digest", lambda: "fixture")
    return data, path


def payload(data, tool="write", inputs=None):
    return {"hookEventName": "pre_tool_use", "sessionId": "child-session", "cwd": data["cwd"],
            "toolName": tool, "toolInput": inputs or {"path": "assigned.txt", "contents": "sample"}}


def test_cross_runtime_mapping_is_idempotent(worker):
    data, _ = worker
    normalized = hooks.normalize_payload(payload(data), "grok")
    assert normalized["session_id"] == "fictional-owner"
    assert normalized["han_runtime"] == "claude"
    assert hooks.normalize_payload(normalized) == normalized
    assert not context.denial(normalized)


def test_worker_loses_access_after_owner_changes(worker):
    data, _ = worker
    Path(data["owner"]["state"], "ACTIVE").write_text('{}')
    with pytest.raises(ValueError, match="ownership"):
        hooks.normalize_payload(payload(data), "grok")


def test_wrong_runtime_cannot_reuse_context(worker):
    data, _ = worker
    with pytest.raises(ValueError, match="runtime"):
        hooks.normalize_payload({"cwd": data["cwd"]}, "codex")


@pytest.mark.parametrize("target", ["outside.txt", "../outside.txt", ".git/config", "state/ACTIVE"])
def test_writes_outside_assigned_scope_are_denied(worker, target):
    data, _ = worker
    normalized = hooks.normalize_payload(payload(data, inputs={"path": target, "contents": "sample"}), "grok")
    assert context.denial(normalized)


def test_read_only_worker_cannot_gain_write_tool(worker):
    data, path = worker
    data["role"] = "code-reviewer"
    path.write_text(json.dumps(data))
    assert context.denial(hooks.normalize_payload(payload(data), "grok"))


def test_probe_acknowledges_only_explicit_denial(worker):
    data, path = worker
    data["phase"] = "probe"
    path.write_text(json.dumps(data))
    normalized = hooks.normalize_payload(payload(data, "Bash", {"command": context.PROBE}), "grok")
    denial = context.denial(normalized)
    assert denial
    context.acknowledge(normalized, denial)
    assert Path(data["guardReady"]).read_text() == "fixture"
