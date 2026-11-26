"""Atomic admission prevents repeated execution and preserves uncertain work."""

from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from chef.workers import store
from chef.workers.routing import RUNTIMES


@pytest.fixture
def job_request(tmp_path):
    project = tmp_path / "fictional-garden"
    project.mkdir()
    return {"project": str(project), "mission": "garden", "unit": "watering",
            "role": "implement", "inputDigest": "a" * 64, "ownerRuntime": "codex",
            "ownerSession": "garden-owner", "prompt": "Implement the specified garden rule."}


@pytest.fixture
def registry(tmp_path):
    return store.Store(tmp_path / "worker-home")


def candidates():
    return [{"runtime": runtime, "available": True, "roles": ["implement"], "tools": []}
            for runtime in RUNTIMES]


def test_concurrent_duplicate_admission_creates_exactly_one_job(registry, job_request):
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda _: registry.reserve(job_request, candidates()), range(16)))
    assert sum(row["created"] for row in records) == 1
    assert len({row["token"] for row in records}) == 1
    assert len(registry.list()) == 1


def test_finished_result_is_recovered_without_rerouting_or_launch(registry, job_request):
    job = registry.reserve(job_request, candidates())
    registry.start(job["key"], job["token"], os.getpid())
    result = {"ok": True, "result": "observed changes", "files": ["garden.py"]}
    registry.finish(job["key"], job["token"], result)
    repeated = store.Store(registry.root).reserve(job_request, [])
    assert repeated["created"] is False
    assert repeated["result"] == result
    with pytest.raises(ValueError, match="already final"):
        registry.finish(job["key"], job["token"], {"ok": False})


def test_dead_worker_stays_reserved_for_inspection_and_never_retries(registry, job_request, monkeypatch):
    job = registry.reserve(job_request, candidates())
    registry.start(job["key"], job["token"], os.getpid())
    monkeypatch.setattr(store, "process_owner_alive", lambda _: False)
    recovered = registry.get(job["key"])
    assert recovered["status"] == "needs-inspection"
    assert registry.reserve(job_request, candidates())["created"] is False
    with pytest.raises(ValueError, match="not available"):
        registry.start(job["key"], job["token"], os.getpid())


def test_native_result_can_complete_after_owner_exits(registry, job_request, monkeypatch):
    job = registry.reserve(job_request, candidates())
    monkeypatch.setattr(store, "process_owner_alive", lambda _: False)
    assert registry.get(job["key"])["status"] == "reserved"
    assert registry.finish(job["key"], job["token"], {"ok": True})["status"] == "completed"


def test_new_parent_cannot_relaunch_same_task_and_recovery_only_transfers_access(registry, job_request):
    job = registry.reserve(job_request, candidates())
    resumed = {**job_request, "ownerSession": "resumed-owner", "ownerRuntime": "grok"}
    with pytest.raises(ValueError, match="explicitly recover"):
        registry.reserve(resumed, candidates())
    registry.recover_owner(job["key"], job["token"], owner_runtime="grok", owner_session="resumed-owner")
    recovered = registry.reserve(resumed, candidates())
    assert recovered["key"] == job["key"] and recovered["created"] is False


def test_cancel_is_a_request_until_execution_reaps_process(registry, job_request):
    job = registry.reserve(job_request, candidates())
    value = registry.cancel(job["key"], job["token"])
    assert value["cancelRequested"] is True and value["status"] == "reserved"
    with pytest.raises(ValueError):
        registry.start(job["key"], job["token"], os.getpid())
    assert registry.finish(job["key"], job["token"], {"ok": False}, "cancelled")["status"] == "cancelled"


def test_tokens_fence_start_result_and_cancellation(registry, job_request):
    job = registry.reserve(job_request, candidates())
    for operation in (lambda: registry.start(job["key"], "wrong", os.getpid()),
                      lambda: registry.finish(job["key"], "wrong", {"ok": True}),
                      lambda: registry.cancel(job["key"], "wrong")):
        with pytest.raises(ValueError, match="ownership"):
            operation()


def test_new_attempt_requires_explicit_changed_request(registry, job_request):
    first = registry.reserve(job_request, candidates())
    registry.finish(first["key"], first["token"], {"ok": False, "partial": ["garden.py"]}, "needs-inspection")
    second = registry.reserve({**job_request, "attempt": 2}, candidates())
    assert second["created"] and second["key"] != first["key"]
    assert registry.get(first["key"])["result"]["partial"] == ["garden.py"]


def test_shared_store_rotates_services_with_unlimited_independent_jobs(registry, job_request):
    records = [registry.reserve({**job_request, "unit": f"watering-{index}"}, candidates()) for index in range(9)]
    assert [job["route"]["runtime"] for job in records] == list(RUNTIMES) * 3


def test_quality_observation_is_once_and_requires_terminal_result(registry, job_request):
    values = [{**candidate, "model": "demo-fixed", "effort": "high"} for candidate in candidates()]
    job = registry.reserve(job_request, values)
    with pytest.raises(ValueError, match="terminal"):
        registry.record_quality(job["key"], job["token"], success=True, rework=0)
    registry.finish(job["key"], job["token"], {"ok": True, "observedModel": "demo-fixed", "observedEffort": "high"})
    assert registry.record_quality(job["key"], job["token"], success=True, rework=1)["samples"] == 1
    with pytest.raises(ValueError, match="already"):
        registry.record_quality(job["key"], job["token"], success=True, rework=0)


@pytest.mark.parametrize("preference,result", [
    ({}, {"ok": True, "observedModel": "demo-fixed", "observedEffort": "high"}),
    ({"model": "inherit", "effort": "high"}, {"ok": True, "observedModel": "demo-fixed", "observedEffort": "high"}),
    ({"model": "demo-fixed", "effort": "high"}, {"ok": True}),
    ({"model": "demo-fixed", "effort": "high"}, {"ok": True, "observedModel": "demo-other", "observedEffort": "high"}),
])
def test_quality_cannot_credit_unknown_or_unobserved_model(registry, job_request, preference, result):
    values = [{**candidate, **preference} for candidate in candidates()]
    job = registry.reserve(job_request, values)
    registry.finish(job["key"], job["token"], result)
    with pytest.raises(ValueError, match="observed model and effort"):
        registry.record_quality(job["key"], job["token"], success=True, rework=0)


def test_route_preferences_are_immutable_after_admission(registry, job_request):
    values = [{**candidate, "model": "demo-fixed", "effort": "high"} for candidate in candidates()]
    job = registry.reserve(job_request, values)
    values[0]["model"] = "demo-other"
    restored = registry.get(job["key"])
    assert restored["route"]["model"] == "demo-fixed"
    assert restored["route"]["effort"] == "high"


def test_corrupt_registry_fails_closed_without_overwriting(registry, job_request):
    registry.path.write_text('{"schema": 99}', encoding="utf-8")
    with pytest.raises(ValueError, match="registry"):
        registry.reserve(job_request, candidates())
    assert json.loads(registry.path.read_text())["schema"] == 99


def test_large_results_and_prompts_live_outside_small_registry(registry, job_request):
    result = {"ok": True, "result": "x" * (2 * 1024 * 1024)}
    records = []
    for index in range(17):
        job = registry.reserve({**job_request, "unit": f"watering-{index}", "prompt": "p" * 100_000}, candidates())
        registry.finish(job["key"], job["token"], result)
        records.append(job)
    assert registry.path.stat().st_size < 50_000
    compact = json.loads(registry.path.read_text(encoding="utf-8"))
    assert all("prompt" not in row["request"] and "result" not in row
               for row in compact["jobs"].values())
    assert registry.get(records[-1]["key"])["result"] == result
    assert len(registry.list()) == 17
    assert all(len(job["request"]["prompt"]) == 100_000 for job in registry.list())


def test_corrupt_result_payload_fails_closed_without_rewriting_index(registry, job_request):
    job = registry.reserve(job_request, candidates())
    registry.finish(job["key"], job["token"], {"ok": True, "result": "observed changes"})
    original = registry.path.read_bytes()
    reference = json.loads(original)["jobs"][job["key"]]["resultRef"]
    payload = registry.root / "payloads" / (reference + ".json")
    payload.write_text('{"ok": false}', encoding="utf-8")
    with pytest.raises(ValueError, match="payload hash"):
        registry.get(job["key"])
    assert registry.path.read_bytes() == original


def test_payload_publish_failure_preserves_previous_complete_record(registry, job_request, monkeypatch):
    job = registry.reserve(job_request, candidates())
    original = registry.path.read_bytes()

    def fail_index_publish(*args, **kwargs):
        raise OSError("simulated index publication failure")

    monkeypatch.setattr(store, "atomic_write_json", fail_index_publish)
    with pytest.raises(OSError, match="publication"):
        registry.finish(job["key"], job["token"], {"ok": True, "result": "changed"})
    assert registry.path.read_bytes() == original
    assert registry.get(job["key"])["status"] == "reserved"


def test_legacy_inline_records_round_trip_and_migrate_on_next_write(registry, job_request):
    job = registry.reserve(job_request, candidates())
    state = registry._read()
    registry._hydrate(state["jobs"][job["key"]])
    registry.path.write_text(json.dumps(state), encoding="utf-8")
    assert registry.get(job["key"])["request"]["prompt"] == job_request["prompt"]
    registry.finish(job["key"], job["token"], {"ok": True})
    assert "requestRef" in json.loads(registry.path.read_text(encoding="utf-8"))["jobs"][job["key"]]


def test_get_and_mutation_only_load_target_payloads(registry, job_request, monkeypatch):
    jobs = []
    for index in range(12):
        job = registry.reserve({**job_request, "unit": f"watering-{index}"}, candidates())
        registry.finish(job["key"], job["token"], {"ok": True, "result": "x" * 1_000_000})
        jobs.append(job)
    reads = []
    writes = []
    original_read, original_write = registry._read_payload, registry._write_payload

    def observed_read(reference, limit):
        reads.append(reference)
        return original_read(reference, limit)

    def observed_write(value, limit):
        writes.append(value)
        return original_write(value, limit)

    monkeypatch.setattr(registry, "_read_payload", observed_read)
    monkeypatch.setattr(registry, "_write_payload", observed_write)
    assert registry.get(jobs[0]["key"])["result"]["ok"] is True
    assert len(reads) == 2 and not writes
    reads.clear()
    assert len(registry.list(metadata=True)) == 12
    assert not reads and not writes
    pending = registry.reserve({**job_request, "unit": "another-unit"}, candidates())
    assert not reads and len(writes) == 1
    reads.clear()
    writes.clear()
    registry.cancel(pending["key"], pending["token"])
    assert len(reads) == 1 and not writes


def test_actual_rate_limit_backoff_skips_only_affected_profile(registry, job_request, monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: 1000)
    values = [{**candidate, "profile": "garden-account"} for candidate in candidates()]
    first = registry.note_rate_limit("claude", {"errorCode": "rate_limited"}, profile="garden-account")
    assert first["retryAfter"] == 1060 and first["reason"] == "rate_limited"
    assert registry.reserve(job_request, values)["route"]["runtime"] == "codex"
    other_profile = [{**values[0], "profile": "orchard-account"}]
    assert registry.reserve({**job_request, "unit": "second"}, other_profile)["route"]["runtime"] == "claude"
    second = registry.note_rate_limit("claude", {"errorCode": "rate_limited"}, profile="garden-account")
    assert second["retryAfter"] == 1120
    registry.note_model_success("claude", profile="garden-account")
    assert registry.reserve({**job_request, "unit": "third"}, values[:1])["route"]["runtime"] == "claude"


def test_cooldown_expires_without_inventing_provider_reset(registry, job_request, monkeypatch):
    monkeypatch.setattr(store.time, "time", lambda: 1000)
    registry.note_rate_limit("claude", {"errorCode": "rate_limited"})
    with pytest.raises(ValueError, match="no available"):
        registry.reserve(job_request, candidates()[:1])
    monkeypatch.setattr(store.time, "time", lambda: 1060)
    assert registry.reserve(job_request, candidates()[:1])["route"]["runtime"] == "claude"


def test_rerouting_tries_each_service_once_and_preserves_failure_history(registry, job_request):
    job = registry.reserve({**job_request, "ownerRuntime": "grok"}, candidates())
    registry.start(job["key"], job["token"], os.getpid())
    evidence = {"errorCode": "rate_limited", "beforeWork": True}
    second = registry.reroute(job["key"], job["token"], candidates(), evidence)
    assert second["route"]["runtime"] == "codex" and second["status"] == "running"
    third = registry.reroute(job["key"], job["token"], candidates(), evidence)
    assert third["route"]["runtime"] == "grok" and third["status"] == "reserved"
    registry.start(job["key"], job["token"], os.getpid())
    with pytest.raises(ValueError, match="no available"):
        registry.reroute(job["key"], job["token"], candidates(), evidence)
    restored = registry.get(job["key"])
    assert [attempt["route"]["runtime"] for attempt in restored["attemptHistory"]] == list(RUNTIMES)
    assert restored["key"] == job["key"] and restored["token"] == job["token"]


def test_partial_work_cancellation_and_foreign_tokens_cannot_reroute(registry, job_request):
    job = registry.reserve(job_request, candidates())
    registry.start(job["key"], job["token"], os.getpid())
    with pytest.raises(ValueError, match="beforeWork"):
        registry.reroute(job["key"], job["token"], candidates(), {"beforeWork": False})
    with pytest.raises(ValueError, match="ownership"):
        registry.reroute(job["key"], "foreign", candidates(), {"beforeWork": True})
    registry.cancel(job["key"], job["token"])
    with pytest.raises(ValueError, match="noncancelled"):
        registry.reroute(job["key"], job["token"], candidates(), {"beforeWork": True})


@pytest.mark.parametrize("field,value", [("project", "relative"), ("ownerRuntime", "invented"),
                                        ("ownerSession", ""), ("role", "invented"), ("inputDigest", "")])
def test_invalid_request_has_no_persisted_admission(registry, job_request, field, value):
    with pytest.raises(ValueError):
        registry.reserve({**job_request, field: value}, candidates())
    assert not registry.path.exists()
