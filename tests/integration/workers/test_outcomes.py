"""Routing evidence comes from the independently validated final file state."""

import hashlib
from types import SimpleNamespace

import pytest

from chef.workers.outcomes import record_completed_unit
from chef.workers.store import Store


@pytest.fixture
def completed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    output = project / "feature.txt"
    output.write_text("implemented", encoding="utf-8")
    fingerprint = hashlib.sha256(output.read_bytes()).hexdigest()
    ctx = SimpleNamespace(project=str(project), mission_id="test-mission")
    store = Store(tmp_path / "workers")
    request = {"project": str(project), "mission": ctx.mission_id, "unit": "unit1",
               "role": "implement", "inputDigest": "before", "ownerRuntime": "codex",
               "ownerSession": "owner", "condition": "small-module"}
    job = store.reserve(request, [{"runtime": "grok", "available": True,
                                  "roles": ["implement"], "tools": [],
                                  "model": "fictional-model", "effort": "high"}])
    store.finish(job["key"], job["token"], {"ok": True, "files": {"feature.txt": fingerprint},
                 "observedModel": "fictional-model", "observedEffort": "high"})
    return ctx, store, output, fingerprint


def quality(store):
    return store._read()["quality"]


def test_validated_current_output_records_one_sample(completed):
    ctx, store, _, fingerprint = completed
    record_completed_unit(ctx, "unit1", {"feature.txt": fingerprint}, store=store)
    record_completed_unit(ctx, "unit1", {"feature.txt": fingerprint}, store=store)
    assert quality(store)[0]["samples"] == 1


def test_worker_output_outside_verified_scope_gets_no_credit(completed):
    ctx, store, _, _ = completed
    record_completed_unit(ctx, "unit1", {"other.txt": "unrelated"}, store=store)
    assert not quality(store)


def test_superseded_output_gets_no_credit(completed):
    ctx, store, output, fingerprint = completed
    output.write_text("changed after worker", encoding="utf-8")
    record_completed_unit(ctx, "unit1", {"feature.txt": fingerprint}, store=store)
    assert not quality(store)


def test_different_unit_gets_no_credit(completed):
    ctx, store, _, fingerprint = completed
    record_completed_unit(ctx, "other-unit", {"feature.txt": fingerprint}, store=store)
    assert not quality(store)


def test_configured_model_without_observed_identity_gets_no_credit(completed, monkeypatch):
    ctx, store, _, fingerprint = completed
    jobs = store.list()
    jobs[0]["result"].pop("observedModel")
    monkeypatch.setattr(store, "list", lambda **kwargs: jobs)
    monkeypatch.setattr(store, "get", lambda *args, **kwargs: jobs[0])
    record_completed_unit(ctx, "unit1", {"feature.txt": fingerprint}, store=store)
    assert not quality(store)
