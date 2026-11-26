import hashlib
import json
from pathlib import Path

import pytest

from chef.workers import observations

SESSION = "00000000-0000-0000-0000-000000000001"


def stream(directory, rows):
    stdout = "\n".join(map(json.dumps, rows))
    (directory / "work.stdout.jsonl").write_text(stdout, encoding="utf-8")
    return stdout


def test_claude_observed_identity_has_hashed_runtime_pointer(tmp_path):
    stdout = stream(tmp_path, [{"type": "system", "subtype": "init", "model": "sample-model", "effort": "high"},
                               {"type": "result", "is_error": False}])
    value = observations.collect("claude", stdout, SESSION, cwd=tmp_path, environment={}, directory=tmp_path)
    assert value["observedModel"] == "sample-model" and value["observedEffort"] == "high"
    evidence = value["runtimeEvidence"]
    assert evidence["modelLine"] == 1 and evidence["effortLine"] == 1
    assert evidence["sha256"] == hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest()


def test_grok_model_usage_reports_model_but_does_not_invent_effort(tmp_path):
    stdout = stream(tmp_path, [{"type": "end", "stopReason": "end_turn",
                               "modelUsage": {"sample-grok": {"inputTokens": 7}}}])
    value = observations.collect("grok", stdout, SESSION, cwd=tmp_path, environment={}, directory=tmp_path)
    assert value["observedModel"] == "sample-grok" and "observedEffort" not in value


@pytest.mark.parametrize("runtime", ["claude", "grok"])
def test_assistant_text_and_missing_runtime_metadata_remain_unknown(tmp_path, runtime):
    stdout = stream(tmp_path, [{"type": "assistant", "model": "claimed-model", "effort": "high"}])
    assert observations.collect(runtime, stdout, SESSION, cwd=tmp_path, environment={}, directory=tmp_path) == {}


def rollout(tmp_path, *, session=SESSION, cwd=None, extra=None):
    root = tmp_path / "selected-profile"
    folder = root / "sessions/2026/01/01"
    folder.mkdir(parents=True)
    path = folder / ("rollout-sample-" + SESSION + ".jsonl")
    rows = [{"type": "session_meta", "payload": {"id": session, "cwd": str(cwd or tmp_path)}},
            {"type": "turn_context", "payload": {"model": "sample-codex", "reasoning_effort": "high"}}]
    if extra:
        rows.extend(extra)
    path.write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
    return root, path


def test_codex_latest_context_in_exact_selected_profile_has_source_evidence(tmp_path):
    home, source = rollout(tmp_path, extra=[{"type": "turn_context", "payload": {
        "model": "sample-codex-new", "effort": "medium"}}])
    value = observations.collect("codex", "", SESSION, cwd=tmp_path,
                                 environment={"CODEX_HOME": str(home)}, directory=tmp_path)
    assert value["observedModel"] == "sample-codex-new" and value["observedEffort"] == "medium"
    evidence = json.loads(Path(value["runtimeEvidence"]["path"]).read_text())
    assert evidence["source"] == str(source) and evidence["session_id"] == SESSION
    with source.open("rb") as file:
        file.seek(evidence["sourceOffset"])
        assert hashlib.sha256(file.readline()).hexdigest() == evidence["sourceLineSha256"]


@pytest.mark.parametrize("mismatch", ["session", "cwd"])
def test_codex_filename_alone_does_not_prove_identity(tmp_path, mismatch):
    options = {"session": "00000000-0000-0000-0000-000000000002"} if mismatch == "session" else {"cwd": tmp_path / "other"}
    home, _ = rollout(tmp_path, **options)
    assert observations.collect("codex", "", SESSION, cwd=tmp_path,
                                environment={"CODEX_HOME": str(home)}, directory=tmp_path) == {}


def test_codex_scan_budget_is_bounded(tmp_path, monkeypatch):
    home, _ = rollout(tmp_path)
    monkeypatch.setattr(observations, "MAX_ENTRIES", 1)
    assert observations.codex_identity(SESSION, tmp_path, {"CODEX_HOME": str(home)}) is None


def test_codex_missing_profile_or_malformed_id_stays_unknown(tmp_path):
    assert observations.codex_identity("not-a-session", tmp_path, {"CODEX_HOME": str(tmp_path)}) is None
    assert observations.codex_identity(SESSION, tmp_path, {"CODEX_HOME": str(tmp_path)}) is None


def test_optional_locked_evidence_does_not_fail_completed_work(tmp_path, monkeypatch):
    stdout = stream(tmp_path, [{"type": "system", "subtype": "init", "model": "sample-model"}])
    monkeypatch.setattr(observations.Path, "read_bytes", lambda _: (_ for _ in ()).throw(PermissionError("fixture locked file")))
    assert observations.collect("claude", stdout, SESSION, cwd=tmp_path, environment={}, directory=tmp_path) == {}
