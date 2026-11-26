"""Synthetic cross-runtime lifecycle checks without live services or credentials."""

import itertools
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.mission.contracts import UNIT_HEADER
from chef.workers import controller
from chef.workers import cli
from chef.workers.store import Store


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("CHEF_WORKERS_MODE", raising=False)
    global_config = tmp_path / "user preferences" / "config.json"
    monkeypatch.setattr(controller.user_config, "path", lambda: global_config)
    project = tmp_path / "fictional orchard"
    project.mkdir()
    (project / "source.txt").write_text("initial", encoding="utf-8")
    state = project / "state"
    folder = state / "sample-mission"
    folder.mkdir(parents=True)
    (folder / "mission.md").write_text("# Fictional mission", encoding="utf-8")
    row = dict.fromkeys(UNIT_HEADER, "sample")
    row["id"] = "U1"
    (folder / "units.tsv").write_text("\t".join(UNIT_HEADER) + "\n" +
                                      "\t".join(row[name] for name in UNIT_HEADER) + "\n", encoding="utf-8")
    adapter = {"schema": 1, "paths": {"stateDir": "state"}}
    monkeypatch.setattr(controller.configuration, "locate", lambda path, runtime: SimpleNamespace(
        project=str(project), adapter_path=str(project / "adapter.json")))
    monkeypatch.setattr(controller.configuration, "read_adapter", lambda *args: adapter)

    def request(owner="claude", role="implement"):
        (state / "ACTIVE").write_text(json.dumps({"id": "sample-mission", "session": "owner",
                                                 "runtime": owner}), encoding="utf-8")
        return {"requestId": "operation-1", "project": str(project), "ownerRuntime": owner, "ownerSession": "owner",
                "mission": "sample-mission", "unit": "U1", "role": role,
                "prompt": "Inspect the assigned fictional file.", "inputPaths": ["source.txt"],
                "files": ["source.txt"], "hostTools": ["Read", "Write"],
                "hostRoles": ["chef:" + name for name in controller.ROLE_KEYS]}

    return SimpleNamespace(root=project, state=state, request=request,
                           store=Store(tmp_path / "worker state"), adapter=adapter, global_config=global_config)


def choose(monkeypatch, target):
    calls = []

    def candidates(adapter, cwd, role, **options):
        calls.append(options)
        return [{"runtime": runtime, "available": runtime == target, "roles": [role], "tools": ["Read", "Write"]}
                for runtime in controller.RUNTIMES]

    monkeypatch.setattr(controller.backends, "candidates", candidates)
    return calls


@pytest.mark.parametrize("owner,target", list(itertools.product(controller.RUNTIMES, repeat=2)))
def test_all_nine_owner_target_routes_are_idempotent(project, monkeypatch, owner, target):
    calls = choose(monkeypatch, target)
    launches = []
    request = project.request(owner)
    first = controller.dispatch(request, store=project.store, launch=lambda *args: launches.append(args))
    second = controller.dispatch(request, store=project.store, launch=lambda *args: launches.append(args))
    assert first["key"] == second["key"]
    assert second["created"] is False
    assert first["native"] is (owner == target)
    assert len(launches) == (0 if owner == target else 1)
    assert calls[0] == {"native": owner, "host_tools": ["Read", "Write"],
                        "allowed_runtimes": list(controller.RUNTIMES)}


def test_native_unregistered_role_cannot_create_unusable_dispatch(project, monkeypatch):
    choose(monkeypatch, "claude")
    request = {**project.request(), "hostRoles": []}
    with pytest.raises(ValueError, match="no available worker"):
        controller.dispatch(request, store=project.store)
    assert project.store.list() == []


@pytest.mark.parametrize("owner,mode", list(itertools.product(controller.RUNTIMES, controller.MODES)))
def test_mode_limits_candidates_before_launch(project, monkeypatch, owner, mode):
    target = owner if mode in ("auto", "native") else mode
    calls = choose(monkeypatch, target)
    job = controller.dispatch({**project.request(owner), "mode": mode}, store=project.store,
                              launch=lambda *args: None)
    allowed = list(controller.RUNTIMES) if mode == "auto" else [target]
    assert calls[0]["allowed_runtimes"] == allowed
    assert job["route"]["runtime"] == target
    assert job["native"] is (owner == target)
    assert cli.summary(job, project.store)["mode"] == mode


@pytest.mark.parametrize("configured,environment,requested,expected", [
    (None, None, None, "auto"), ("native", None, None, "native"),
    ("native", "claude", None, "claude"), ("native", "claude", "auto", "auto"),
])
def test_mode_precedence_and_durable_pin(project, monkeypatch, configured, environment, requested, expected):
    if configured is not None:
        project.adapter["workers"] = {"mode": configured}
    if environment is not None:
        monkeypatch.setenv("CHEF_WORKERS_MODE", environment)
    request = project.request()
    if requested is not None:
        request["mode"] = requested
    prepared, adapter, owner = controller.prepare(request)
    assert prepared["mode"] == expected
    monkeypatch.setenv("CHEF_WORKERS_MODE", "grok")
    calls = choose(monkeypatch, "claude")
    controller.available_candidates(adapter, prepared, owner)
    assert calls[0]["allowed_runtimes"] == prepared["allowedRuntimes"]


@pytest.mark.parametrize("source,invalid", [(source, value)
    for source in ("request", "environment", "adapter")
    for value in ("off", "", None, 1, [], {}) if source != "environment" or isinstance(value, str)])
def test_invalid_mode_fails_before_candidates_or_admission(project, monkeypatch, source, invalid):
    request = project.request()
    if source == "environment":
        monkeypatch.setenv("CHEF_WORKERS_MODE", invalid)
    elif source == "adapter":
        project.adapter["workers"] = {"mode": invalid}
    else:
        request["mode"] = invalid
    monkeypatch.setattr(controller.backends, "candidates", lambda *a, **k: pytest.fail("must not probe"))
    with pytest.raises(ValueError, match="worker mode"):
        controller.dispatch(request, store=project.store)
    assert project.store.list() == []


def test_mode_change_cannot_reuse_submitted_request_id(project, monkeypatch):
    choose(monkeypatch, "claude")
    request = project.request()
    controller.dispatch({**request, "mode": "auto"}, store=project.store)
    with pytest.raises(ValueError, match="different intent"):
        controller.dispatch({**request, "mode": "native"}, store=project.store)


@pytest.mark.parametrize("owner", controller.RUNTIMES)
def test_global_mode_applies_across_hosts_and_is_pinned_for_existing_work(project, monkeypatch, owner):
    project.global_config.parent.mkdir()
    project.global_config.write_text('{"workers":{"mode":"native"}}', encoding="utf-8")
    choose(monkeypatch, owner)
    request = project.request(owner=owner)
    job = controller.dispatch(request, store=project.store)
    assert job["native"] and job["request"]["allowedRuntimes"] == [owner]
    project.global_config.write_text('{"workers":{"mode":"auto"}}', encoding="utf-8")
    assert controller.status(job["key"], store=project.store)["request"]["mode"] == "native"
    project.store.finish(job["key"], job["token"], {"ok": True})
    next_job = controller.dispatch({**request, "requestId": "next-work"}, store=project.store)
    assert next_job["request"]["mode"] == "auto"
    assert next_job["request"]["allowedRuntimes"] == list(controller.RUNTIMES)


@pytest.mark.parametrize("layer", ["request", "environment", "adapter"])
def test_explicit_mode_overrides_even_broken_unused_global_config(project, monkeypatch, layer):
    project.global_config.parent.mkdir()
    project.global_config.write_text("{unfinished", encoding="utf-8")
    request = project.request()
    if layer == "request":
        request["mode"] = "native"
    elif layer == "environment":
        monkeypatch.setenv("CHEF_WORKERS_MODE", "native")
    else:
        project.adapter["workers"] = {"mode": "native"}
    choose(monkeypatch, "claude")
    assert controller.dispatch(request, store=project.store)["request"]["mode"] == "native"


@pytest.mark.parametrize("configuration", ["{unfinished", "[]", '{"workers":[]}', '{"workers":{"mode":"off"}}'])
def test_invalid_selected_global_config_never_probes_or_admits_work(project, monkeypatch, configuration):
    project.global_config.parent.mkdir()
    project.global_config.write_text(configuration, encoding="utf-8")
    monkeypatch.setattr(controller.backends, "candidates", lambda *a, **k: pytest.fail("must not probe"))
    with pytest.raises(ValueError):
        controller.dispatch(project.request(), store=project.store)
    assert project.store.list() == []


def test_unavailable_selected_service_does_not_use_available_alternative(project, monkeypatch):
    choose(monkeypatch, "codex")
    with pytest.raises(ValueError, match="no available worker"):
        controller.dispatch({**project.request(), "mode": "claude",
                             "allowedRuntimes": list(controller.RUNTIMES)}, store=project.store,
                            launch=lambda *args: pytest.fail("must not launch"))
    assert project.store.list() == []


def test_changed_mode_overrides_previous_service_continuity(project, monkeypatch):
    quota_candidates(monkeypatch)
    request = project.request(owner="grok")
    original = controller.dispatch({**request, "mode": "claude"}, store=project.store, launch=lambda *args: None)
    project.store.finish(original["key"], original["token"], {"ok": True, "session_id": "prior-claude-session"})
    followup = controller.dispatch({**request, "requestId": "followup", "mode": "codex", "previous": original["key"]},
                                   store=project.store, launch=lambda *args: None)
    assert followup["route"]["runtime"] == "codex"
    assert "resumeSession" not in followup["request"]


def test_single_service_mode_cannot_escape_to_available_host_after_quota(project, monkeypatch):
    quota_candidates(monkeypatch)
    request = {**project.request(owner="codex"), "mode": "claude"}
    job = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append(runtime)
        raise controller.backends.RateLimited(runtime, {"errorCode": "rate_limited", "error": "quota"})

    monkeypatch.setattr(controller.backends, "run", run)
    monkeypatch.setenv("CHEF_WORKERS_MODE", "auto")
    failed = controller.supervise(job["key"], job["token"], store=project.store)
    assert failed["status"] == "failed"
    assert failed["result"]["errorCode"] == "rate_limited"
    assert calls == ["claude"]


def test_cli_mode_overrides_request_without_modifying_file(project, monkeypatch, capsys):
    choose(monkeypatch, "claude")
    path = project.root / "request.json"
    path.write_text(json.dumps({**project.request(), "mode": "grok"}), encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr("sys.argv", ["worker.py", "--home", str(project.store.root),
                                    "dispatch", "--request", str(path), "--mode", "native"])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["native"] and result["mode"] == "native"
    assert path.read_bytes() == original


def test_project_audit_role_is_preserved_for_native_spawn(project, monkeypatch):
    project.adapter["roles"] = {"review": "example-reviewer", "audit": "example-auditor"}
    observations = []

    def candidates(adapter, cwd, role, **options):
        observations.append(adapter["roles"])
        return [{"runtime": "claude", "available": True, "roles": [role], "tools": []}]

    monkeypatch.setattr(controller.backends, "candidates", candidates)
    job = controller.dispatch({**project.request(role="audit"), "hostRoles": ["example-auditor"]}, store=project.store)
    assert job["native"] is True
    assert job["actualRole"] == "example-auditor"
    assert observations[0]["review"] == "example-auditor"


def test_observed_host_only_custom_role_needs_no_foreign_translation(project, monkeypatch):
    project.adapter["roles"] = {"implement": "example-native-builder"}
    monkeypatch.setattr(controller.backends, "candidates", lambda *a, **k: [
        {"runtime": runtime, "available": False, "roles": ["dev-medium"], "tools": [],
         "error": "custom role has no foreign definition"} for runtime in controller.RUNTIMES])
    job = controller.dispatch({**project.request(), "hostRoles": ["example-native-builder"]}, store=project.store)
    assert job["native"] is True
    assert job["actualRole"] == "example-native-builder"
    assert job["route"].get("policy") == ""


def test_supervisor_executes_reserved_request_and_persists_result(project, monkeypatch):
    choose(monkeypatch, "grok")
    job = controller.dispatch(project.request(), store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, role, prompt, **kwargs):
        calls.append((runtime, role, kwargs))
        assert kwargs["cancelled"]() is False
        assert Path(kwargs["owner"]["cancelPath"]).parent == controller.attempt_directory(project.store, job)
        kwargs["on_start"](os.getpid())
        return {"ok": True, "result": "Observed result", "exit_code": 0}

    monkeypatch.setattr(controller.backends, "run", run)
    result = controller.supervise(job["key"], job["token"], store=project.store)
    assert result["status"] == "completed"
    assert calls[0][0:2] == ("grok", "dev-medium")
    assert controller.status(job["key"], store=project.store)["result"]["result"] == "Observed result"
    assert len(calls) == 1


def test_changed_output_before_launch_never_starts_backend(project, monkeypatch):
    choose(monkeypatch, "grok")
    request = project.request()
    request["inputPaths"] = []
    job = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    (project.root / "source.txt").write_text("user changed", encoding="utf-8")
    monkeypatch.setattr(controller.backends, "run", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="output files changed"):
        controller.supervise(job["key"], job["token"], store=project.store)
    assert project.store.get(job["key"])["status"] == "needs-inspection"


def test_native_requires_existing_evidence_and_does_not_change_mission(project, monkeypatch):
    choose(monkeypatch, "claude")
    request = project.request()
    before = (project.state / "ACTIVE").read_bytes()
    job = controller.dispatch(request, store=project.store)
    with pytest.raises(ValueError, match="requires evidence"):
        controller.native_result(job["key"], job["token"], {"ok": True}, store=project.store)
    result = controller.native_result(job["key"], job["token"],
                                      {"ok": True, "evidence": ["source.txt"]}, store=project.store)
    assert result["status"] == "completed"
    assert result["result"]["evidence"]["source.txt"]
    assert (project.state / "ACTIVE").read_bytes() == before


def test_review_receipt_rejects_changed_inputs(project, monkeypatch):
    choose(monkeypatch, "claude")
    job = controller.dispatch(project.request(role="review"), store=project.store)
    (project.root / "source.txt").write_text("concurrent write", encoding="utf-8")
    with pytest.raises(ValueError, match="review inputs changed"):
        controller.native_result(job["key"], job["token"],
                                 {"ok": True, "evidence": ["source.txt"]}, store=project.store)
    assert project.store.get(job["key"])["status"] == "needs-inspection"
    assert controller.reconcile(job["key"], job["token"], ["source.txt"], store=project.store)["status"] == "failed"


def test_conflicting_file_job_is_rejected_but_other_files_can_start(project, monkeypatch):
    choose(monkeypatch, "grok")
    first = project.request()
    controller.dispatch(first, store=project.store, launch=lambda *args: None)
    with pytest.raises(ValueError, match="scope conflicts"):
        controller.dispatch({**first, "requestId": "operation-2", "prompt": "Another overlapping task"}, store=project.store,
                            launch=lambda *args: None)
    result = controller.dispatch({**first, "requestId": "operation-3", "prompt": "Independent task", "inputPaths": [], "files": ["other.txt"]},
                                  store=project.store, launch=lambda *args: None)
    assert result["created"] is True


@pytest.mark.parametrize("difference", [{"timeout": 40}, {"attempt": 2}, {"hostTools": ["Read"]}])
def test_changed_request_identity_does_not_bypass_file_conflict(project, monkeypatch, difference):
    choose(monkeypatch, "grok")
    first = project.request()
    controller.dispatch(first, store=project.store, launch=lambda *args: None)
    with pytest.raises(ValueError, match="scope conflicts"):
        controller.dispatch({**first, "requestId": "operation-2", **difference}, store=project.store, launch=lambda *args: None)


def test_dotdot_alias_and_hardlink_cannot_bypass_file_conflict(project, monkeypatch):
    choose(monkeypatch, "grok")
    (project.root / "nested").mkdir()
    first = project.request()
    controller.dispatch(first, store=project.store, launch=lambda *args: None)
    for alias in ("nested/../source.txt", "source-alias.txt"):
        if alias == "source-alias.txt":
            try:
                os.link(project.root / "source.txt", project.root / alias)
            except OSError:
                continue
        with pytest.raises(ValueError, match="scope conflicts"):
            controller.dispatch({**first, "requestId": "operation-2", "prompt": "Aliased write", "files": [alias], "inputPaths": []},
                                store=project.store, launch=lambda *args: None)


def test_owner_or_unknown_unit_rejected_before_candidate_probe(project, monkeypatch):
    request = project.request()
    monkeypatch.setattr(controller.backends, "candidates", lambda *a, **k: pytest.fail("not admitted"))
    with pytest.raises(ValueError, match="ACTIVE"):
        controller.dispatch({**request, "ownerSession": "foreign"}, store=project.store)
    with pytest.raises(Exception, match="U2"):
        controller.dispatch({**request, "unit": "U2"}, store=project.store)


def test_cancel_is_requested_until_backend_is_reaped(project, monkeypatch):
    choose(monkeypatch, "grok")
    job = controller.dispatch(project.request(), store=project.store, launch=lambda *args: None)
    result = controller.cancel(job["key"], job["token"], store=project.store)
    assert result["cancellationRequested"] is True
    assert result["status"] == "reserved"
    seen = []

    def run(*args, **kwargs):
        seen.append(kwargs["cancelled"]())
        return {"ok": False, "error": "cancelled after reaping"}

    monkeypatch.setattr(controller.backends, "run", run)
    result = controller.supervise(job["key"], job["token"], store=project.store)
    assert seen == [True]
    assert result["status"] == "cancelled"


def test_failed_partial_write_is_preserved_for_inspection(project, monkeypatch):
    choose(monkeypatch, "grok")
    job = controller.dispatch(project.request(), store=project.store, launch=lambda *args: None)

    def run(*args, **kwargs):
        (project.root / "source.txt").write_text("partial", encoding="utf-8")
        return {"ok": False, "error": "limit reached"}

    monkeypatch.setattr(controller.backends, "run", run)
    result = controller.supervise(job["key"], job["token"], store=project.store)
    assert result["status"] == "needs-inspection"
    assert (project.root / "source.txt").read_text(encoding="utf-8") == "partial"


def test_exact_resubmission_after_successful_edits_reuses_completed_job(project, monkeypatch):
    choose(monkeypatch, "grok")
    launches = []
    request = project.request()
    job = controller.dispatch(request, store=project.store, launch=lambda *args: launches.append(args))

    def run(*args, **kwargs):
        (project.root / "source.txt").write_text("implemented", encoding="utf-8")
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(controller.backends, "run", run)
    controller.supervise(job["key"], job["token"], store=project.store)
    repeated = controller.dispatch(request, store=project.store, launch=lambda *args: launches.append(args))
    assert repeated["created"] is False
    assert repeated["key"] == job["key"]
    assert repeated["status"] == "completed"
    assert len(launches) == 1


def test_same_request_id_cannot_change_intent(project, monkeypatch):
    choose(monkeypatch, "grok")
    request = project.request()
    controller.dispatch(request, store=project.store, launch=lambda *args: None)
    with pytest.raises(ValueError, match="different intent"):
        controller.dispatch({**request, "prompt": "Different task"}, store=project.store, launch=lambda *args: None)


def test_correction_resumes_observed_same_policy_session(project, monkeypatch):
    choose(monkeypatch, "grok")
    request = project.request()
    prior = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    project.store.finish(prior["key"], prior["token"], {"ok": True, "session_id": "observed-session"})
    correction = controller.dispatch({**request, "requestId": "correction-1", "previous": prior["key"]},
                                     store=project.store, launch=lambda *args: None)
    assert correction["route"]["runtime"] == "grok"
    assert correction["request"]["resumeSession"] == "observed-session"
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["owner"])
        return {"ok": True}

    monkeypatch.setattr(controller.backends, "run", run)
    controller.supervise(correction["key"], correction["token"], store=project.store)
    assert calls[0]["resumeSession"] == "observed-session"


def test_independent_work_never_resumes_previous_session(project, monkeypatch):
    choose(monkeypatch, "grok")
    request = project.request(role="review")
    prior = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    project.store.finish(prior["key"], prior["token"], {"ok": True, "session_id": "observed-session"})
    review = controller.dispatch({**request, "requestId": "review-2", "previous": prior["key"], "independent": True},
                                 store=project.store, launch=lambda *args: None)
    assert "resumeSession" not in review["request"]
    assert review["route"]["fresh"] is True


def test_dead_creator_without_launch_marker_is_inspection_not_a_retry(project, monkeypatch):
    choose(monkeypatch, "grok")
    launches = []
    request = project.request()
    job = controller.dispatch(request, store=project.store, launch=lambda *args: launches.append(args))
    monkeypatch.setattr(controller, "process_owner_alive", lambda record: False)
    status = controller.status(job["key"], store=project.store)
    assert status["status"] == "needs-inspection"
    again = controller.dispatch(request, store=project.store, launch=lambda *args: launches.append(args))
    assert again["key"] == job["key"]
    assert len(launches) == 1


def test_reconcile_preserves_partial_edits_and_releases_scope(project, monkeypatch):
    choose(monkeypatch, "grok")
    request = project.request()
    job = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    (project.root / "source.txt").write_text("partial", encoding="utf-8")
    project.store.finish(job["key"], job["token"], {"ok": False}, status="needs-inspection")
    resolved = controller.reconcile(job["key"], job["token"], ["source.txt"], store=project.store)
    assert resolved["status"] == "failed"
    assert resolved["result"]["inspection"]["source.txt"]
    next_job = controller.dispatch({**request, "requestId": "followup"}, store=project.store, launch=lambda *args: None)
    assert next_job["created"] is True
    assert (project.root / "source.txt").read_text(encoding="utf-8") == "partial"


def test_reconcile_rejects_live_process_and_uncollected_native(project, monkeypatch):
    choose(monkeypatch, "grok")
    job = controller.dispatch(project.request(), store=project.store, launch=lambda *args: None)
    project.store.start(job["key"], job["token"], os.getpid())
    with pytest.raises(ValueError, match="still alive"):
        controller.reconcile(job["key"], job["token"], ["source.txt"], store=project.store)
    project.store.finish(job["key"], job["token"], {"ok": False})
    choose(monkeypatch, "claude")
    native = controller.dispatch({**project.request(), "requestId": "native-followup"}, store=project.store)
    with pytest.raises(ValueError, match="collect the native"):
        controller.reconcile(native["key"], native["token"], ["source.txt"], store=project.store)


def test_slow_auth_probe_does_not_hold_shared_admission(project, monkeypatch):
    slow_started = threading.Event()
    release = threading.Event()

    def candidates(adapter, cwd, role, **options):
        if "slow-helper" in options["host_tools"]:
            slow_started.set()
            assert release.wait(20)
        return [{"runtime": "grok", "available": True, "roles": [role], "tools": []}]

    monkeypatch.setattr(controller.backends, "candidates", candidates)
    base = project.request()
    slow = {**base, "requestId": "slow", "inputPaths": [], "files": ["slow.txt"], "hostTools": ["slow-helper"]}
    fast = {**base, "requestId": "fast", "inputPaths": [], "files": ["fast.txt"]}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(controller.dispatch, slow, store=project.store, launch=lambda *args: None)
        try:
            assert slow_started.wait(10)
            second = executor.submit(controller.dispatch, fast, store=project.store, launch=lambda *args: None)
            assert second.result(timeout=8)["created"] is True
        finally:
            release.set()
        assert first.result(timeout=10)["created"] is True


def quota_candidates(monkeypatch):
    monkeypatch.setattr(controller.backends, "candidates", lambda adapter, cwd, role, **options: [
        {"runtime": runtime, "available": runtime in ("claude", "codex"), "roles": [role], "tools": []}
        for runtime in controller.RUNTIMES])


def test_probe_quota_reroutes_same_job_and_avoids_known_limited_service(project, monkeypatch):
    quota_candidates(monkeypatch)
    job = controller.dispatch(project.request(owner="grok"), store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append(runtime)
        if runtime == "claude":
            raise controller.backends.RateLimited(runtime, {"errorCode": "rate_limited", "error": "known quota response"})
        return {"ok": True, "result": "done"}

    monkeypatch.setattr(controller.backends, "run", run)
    result = controller.supervise(job["key"], job["token"], store=project.store)
    assert result["key"] == job["key"]
    assert result["status"] == "completed"
    assert result["route"]["runtime"] == "codex"
    assert calls == ["claude", "codex"]
    assert result["attemptHistory"][0]["evidence"]["errorCode"] == "rate_limited"
    followup = controller.dispatch({**project.request(owner="grok"), "requestId": "fresh-work"},
                                   store=project.store, launch=lambda *args: None)
    assert followup["route"]["runtime"] == "codex"


def test_quota_reroute_drops_other_service_resume_session(project, monkeypatch):
    quota_candidates(monkeypatch)
    request = project.request(owner="grok")
    original = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    project.store.finish(original["key"], original["token"], {"ok": True, "session_id": "original-claude-session"})
    correction = controller.dispatch({**request, "requestId": "correction", "previous": original["key"]},
                                     store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append((runtime, kwargs["owner"].get("resumeSession")))
        if runtime == "claude":
            raise controller.backends.RateLimited(runtime, {"errorCode": "rate_limited", "error": "quota"})
        return {"ok": True}

    monkeypatch.setattr(controller.backends, "run", run)
    controller.supervise(correction["key"], correction["token"], store=project.store)
    assert calls == [("claude", "original-claude-session"), ("codex", None)]


def test_initial_health_selection_drops_other_service_resume_session(project, monkeypatch):
    quota_candidates(monkeypatch)
    request = project.request(owner="grok")
    original = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    project.store.finish(original["key"], original["token"], {"ok": True, "session_id": "original-claude-session"})
    project.store.note_rate_limit("claude", {"errorCode": "rate_limited", "error": "observed quota"})
    correction = controller.dispatch({**request, "requestId": "correction", "previous": original["key"]},
                                     store=project.store, launch=lambda *args: None)
    assert correction["route"]["runtime"] == "codex"
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append((runtime, kwargs["owner"].get("resumeSession")))
        return {"ok": True}

    monkeypatch.setattr(controller.backends, "run", run)
    controller.supervise(correction["key"], correction["token"], store=project.store)
    assert calls == [("codex", None)]


def test_quota_fallback_to_owner_is_native_handoff_not_completion(project, monkeypatch):
    quota_candidates(monkeypatch)
    job = controller.dispatch(project.request(owner="codex"), store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append(runtime)
        raise controller.backends.RateLimited(runtime, {"errorCode": "rate_limited", "error": "quota"})

    monkeypatch.setattr(controller.backends, "run", run)
    handoff = controller.supervise(job["key"], job["token"], store=project.store)
    assert calls == ["claude"]
    assert handoff["status"] == "reserved"
    assert handoff["native"] is True
    assert controller.wait(job["key"], seconds=1, store=project.store)["native"] is True


def test_all_probe_quota_failures_are_retained_without_repeat_attempt(project, monkeypatch):
    quota_candidates(monkeypatch)
    job = controller.dispatch(project.request(owner="grok"), store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append(runtime)
        raise controller.backends.RateLimited(runtime, {"errorCode": "rate_limited", "error": "quota"})

    monkeypatch.setattr(controller.backends, "run", run)
    result = controller.supervise(job["key"], job["token"], store=project.store)
    assert calls == ["claude", "codex"]
    assert result["status"] == "failed"
    assert result["result"]["errorCode"] == "rate_limited"
    assert len(result["attemptHistory"]) == 2


def test_probe_quota_does_not_reroute_after_partial_changes(project, monkeypatch):
    quota_candidates(monkeypatch)
    job = controller.dispatch(project.request(owner="grok"), store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append(runtime)
        (project.root / "source.txt").write_text("unexpected partial edit", encoding="utf-8")
        raise controller.backends.RateLimited(runtime, {"errorCode": "rate_limited", "error": "quota"})

    monkeypatch.setattr(controller.backends, "run", run)
    with pytest.raises(ValueError, match="cannot safely reroute"):
        controller.supervise(job["key"], job["token"], store=project.store)
    assert calls == ["claude"]
    assert project.store.get(job["key"])["status"] == "needs-inspection"


def test_work_phase_quota_updates_health_and_preserves_partial_scope(project, monkeypatch):
    quota_candidates(monkeypatch)
    request = project.request(owner="grok")
    job = controller.dispatch(request, store=project.store, launch=lambda *args: None)
    calls = []

    def run(runtime, *args, **kwargs):
        calls.append(runtime)
        (project.root / "source.txt").write_text("partial implementation", encoding="utf-8")
        return {"ok": False, "errorCode": "rate_limited", "error": "quota reached during work"}

    monkeypatch.setattr(controller.backends, "run", run)
    failed = controller.supervise(job["key"], job["token"], store=project.store)
    assert calls == ["claude"]
    assert failed["status"] == "needs-inspection"
    assert failed["result"]["errorCode"] == "rate_limited"
    assert (project.root / "source.txt").read_text(encoding="utf-8") == "partial implementation"
    choose(monkeypatch, "codex")
    controller.dispatch({**request, "requestId": "holding-codex", "files": ["holding.txt"], "inputPaths": []},
                        store=project.store, launch=lambda *args: None)
    quota_candidates(monkeypatch)
    next_job = controller.dispatch({**request, "requestId": "independent-work", "files": ["next.txt"], "inputPaths": []},
                                   store=project.store, launch=lambda *args: None)
    assert next_job["route"]["runtime"] == "codex"
    with pytest.raises(ValueError, match="scope conflicts"):
        controller.dispatch({**request, "requestId": "overlapping-work"}, store=project.store, launch=lambda *args: None)


def test_cli_summary_bounds_results_and_omits_full_pending_request(project, monkeypatch):
    choose(monkeypatch, "grok")
    job = controller.dispatch(project.request(), store=project.store, launch=lambda *args: None)
    pending = cli.summary(job, project.store)
    assert "request" not in pending
    assert "brief" not in pending
    completed = project.store.finish(job["key"], job["token"], {"ok": True, "result": "x" * 10000})
    result = cli.summary(completed, project.store)
    assert len(result["result"]["result"]) == 2000
    assert result["result"]["truncated"] is True
    assert json.loads(Path(result["details"]).read_text(encoding="utf-8"))["result"] == "x" * 10000


def test_recover_owner_validates_current_active_without_relaunch(project, monkeypatch):
    choose(monkeypatch, "claude")
    job = controller.dispatch(project.request(), store=project.store)
    with pytest.raises(ValueError, match="ACTIVE"):
        controller.recover_owner(job["key"], job["token"], "codex", "new-owner", store=project.store)
    (project.state / "ACTIVE").write_text(json.dumps({"id": "sample-mission", "session": "new-owner",
                                                     "runtime": "codex"}), encoding="utf-8")
    result = controller.recover_owner(job["key"], job["token"], "codex", "new-owner", store=project.store)
    assert result["status"] == "reserved"
    assert result["request"]["ownerSession"] == "new-owner"


@pytest.mark.parametrize("field,value", [("project", "relative"), ("timeout", True), ("files", ["../escape"]),
                                        ("requiredTools", "Bash"), ("files", [".git/config"])])
def test_invalid_requests_cannot_reserve_jobs(project, field, value):
    with pytest.raises((ValueError, OSError)):
        controller.prepare({**project.request(), field: value})
    assert project.store.list() == []
