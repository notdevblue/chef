"""Exercise the bundled entry point against an isolated fictional project."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from chef.guards import mission
from chef.mission.contracts import StateError
from chef.mission.documents import share_documents, shared_documents
from chef.verification.contracts import digest
from chef.verification.plan import snapshot
from chef.verification import reviews
from chef.workers import cli as worker_cli, controller
from chef.workers.store import Store
from tests.paths import PLUGIN_ROOT


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty-registry"))
    monkeypatch.setenv("CHEF_WORKERS_HOME", str(tmp_path / "unused-worker-state"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    project = tmp_path / "fictional project"
    adapter = project / ".claude/orchestrator"
    adapter.mkdir(parents=True)
    (adapter / "adapter.json").write_text(json.dumps({
        "schema": 1, "paths": {"stateDir": "state"}, "documents": {"schemaCatalog": "schema.json"},
        "verify": {"grades": ["pytest-pass"]},
    }), encoding="utf-8")
    folder = project / "state" / "orchard"
    folder.mkdir(parents=True)
    (folder / "mission.md").write_text("# Orchard\n", encoding="utf-8")
    mission.write_active(str(folder.parent), mission.Active("orchard", "fictional", "original", runtime="codex"))
    (project / "schema.json").write_text(json.dumps({"schema": 1, "tables": [
        {"kind": "db", "name": "Tree", "fields": {"Id": "int"}},
        {"kind": "proto", "name": "Species", "fields": {"Key": "str"}},
    ]}), encoding="utf-8")
    return project, folder


def run(project, *args, input=None, runtime="codex"):
    options = ("--runtime", runtime) if runtime else ()
    return subprocess.run(
        [sys.executable, "-I", "-B", str(PLUGIN_ROOT / "chef_cli.py"), *args, *options],
        cwd=project, env={**os.environ, "PYTHONUTF8": "1"}, input=json.dumps(input) if input is not None else None,
        capture_output=True, encoding="utf-8", timeout=20,
    )


def tree_block():
    return {"id": "tree", "type": "schema-table", "title": "Tree fields",
            "target": {"kind": "db", "name": "Tree", "state": "existing"},
            "fields": [{"name": "Id", "type": "int"}]}


def test_context_and_document_share_checks_the_registered_bytes(project):
    root, folder = project
    context = run(root, "context", "--session", "fictional")
    assert context.returncode == 0, context.stderr
    assert json.loads(context.stdout)["ownerMatches"] is True

    draft = run(root, "document", "create", "--kind", "report", "--title", "Orchard report")
    assert draft.returncode == 0, draft.stderr
    assert run(root, "document", "share", "report.md").returncode == 1
    assert run(root, "document", "list", "--json").stdout.strip() == "[]"
    report = folder / "report.md"
    report.write_text(report.read_text(encoding="utf-8").replace(
        "## 지금", "## 지금\nReady").replace("## 남은 확인", "## 남은 확인\nNone")
        .replace("## 다음 일", "## 다음 일\nPlant the tree"), encoding="utf-8")
    shared = run(root, "document", "share", "report.md", "--json")
    assert shared.returncode == 0, shared.stderr
    assert json.loads(shared.stdout)["documents"][0]["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    listed = json.loads(run(root, "document", "list", "--json").stdout)[0]
    assert listed["sharing"] == "shared" and listed["issues"] == [] and listed["approval"] is None
    assert listed["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    report.write_text("# Changed report\n", encoding="utf-8")
    assert json.loads(run(root, "document", "check", "report.md").stdout)[0]["sharing"] == "unshared"


def test_edit_during_document_validation_does_not_register_stale_bytes(project):
    _, folder = project
    report = folder / "report.md"
    report.write_text("# Original\n", encoding="utf-8")

    def validate(_name, _path, content):
        assert content.startswith(b"# Original")
        report.write_text("# Changed\n", encoding="utf-8")

    with pytest.raises(StateError, match="검사 중 바뀌었습니다"):
        share_documents(folder, ["report.md"], check_content=validate)
    assert not shared_documents(folder)


def test_normal_init_yields_one_managed_report_and_a_valid_design_template(project):
    root, folder = project
    started = run(root, "mission", "init", "--tier", "M", "--goal", "Review storage")
    assert started.returncode == 0, started.stderr
    assert "chef:document report" in (folder / "report.md").read_text(encoding="utf-8")
    checked = run(root, "document", "share", "report.md")
    assert checked.returncode == 1 and "empty section" in checked.stderr
    duplicate = run(root, "document", "create", "--kind", "report", "--title", "Different title")
    assert duplicate.returncode == 1 and "already exists" in duplicate.stderr
    template = run(root, "question", "template", "--kind", "design", "--id", "confirm-A2")
    assert template.returncode == 0, template.stderr
    draft = json.loads(template.stdout)
    preflight = run(root, "question", "preflight", "--input", "-", "--preview", input=draft)
    assert preflight.returncode == 0, preflight.stderr
    assert preflight.stdout.startswith("# ") and "revision 1" in preflight.stderr
    assert not (folder / "confirm-A2.md").exists()
    published = run(root, "question", "publish", "--input", "-", input=draft)
    assert published.returncode == 0, published.stderr
    snapshot = (folder / "questions/confirm-A2.json").read_bytes()
    template = run(root, "question", "reply-template", "confirm-A2.md")
    assert template.returncode == 0, template.stderr
    reply = json.loads(template.stdout)
    assert reply["id"] and reply["revision"] == 1 and datetime.fromisoformat(reply["at"]).utcoffset().total_seconds() == 0
    assert reply["text"] == "" and reply["updates"] == []
    assert (folder / "questions/confirm-A2.json").read_bytes() == snapshot
    reply.update(text="Blue", updates=[{"id": "color", "status": "answered", "answer": "Blue", "selected": "blue"}])
    answered = run(root, "question", "answer", "confirm-A2.md", "--input", "-", input=reply)
    assert answered.returncode == 0, answered.stderr
    assert run(root, "question", "check").returncode == 0
    checked = run(root, "document", "compare", str(folder / "questions/confirm-A2.json"))
    assert checked.returncode == 0 and json.loads(checked.stdout)["issues"] == [], checked.stderr


def test_direct_question_answer_keeps_exact_text_and_checks_revision_and_retries(project):
    root, folder = project
    template = run(root, "question", "template", "--kind", "design", "--id", "confirm-A2")
    assert template.returncode == 0, template.stderr
    visible = run(root, "question", "publish", "--input", "-", input=json.loads(template.stdout))
    assert visible.returncode == 0, visible.stderr
    path = folder / "questions/confirm-A2.json"
    original = path.read_bytes()
    raw = "Choose blue.\nPlease keep the earlier requirement."
    args = ("question", "answer", "confirm-A2.md", "--text", raw, "--reply-id", "reply-one",
            "--select", "color", "blue", "Blue")
    wrong = run(root, *args, "--expect-revision", "2")
    assert wrong.returncode == 1 and path.read_bytes() == original
    first = run(root, *args, "--expect-revision", "1")
    assert first.returncode == 0, first.stderr
    after = json.loads(path.read_bytes())
    assert after["answers"][0]["text"] == raw
    assert after["answers"][0]["updates"][0]["selected"] == "blue"
    assert "approval" not in after["answers"][0]
    assert after["approval"] == json.loads(original)["approval"]
    repeat = run(root, *args, "--expect-revision", "1")
    assert repeat.returncode == 0 and json.loads(path.read_bytes()) == after, repeat.stderr


def test_unit_batch_admits_stdin_without_a_temporary_payload_file(project):
    root, folder = project
    started = run(root, "mission", "init", "--tier", "M", "--goal", "Plant")
    assert started.returncode == 0, started.stderr
    record = {"schema": 1, "units": [{"id": "M1", "goal": "Plant", "files": "garden.txt", "done": "Planted",
                                    "verify": "Inspect the garden", "timebox": 15, "forbid": "No outside edits"}]}
    result = run(root, "unit", "add", "--input", "-", input=record)
    assert result.returncode == 0, result.stderr
    assert (folder / "units.tsv").is_file()
    assert list(root.glob("*.json")) == [root / "schema.json"]


def init_unit(root, files='["garden.txt", "seedlings.txt"]'):
    started = run(root, "mission", "init", "--tier", "M", "--goal", "Plant")
    assert started.returncode == 0, started.stderr
    (root / "garden.txt").write_text("garden\n", encoding="utf-8")
    result = run(root, "unit", "add", "--input", "-", input={
        "schema": 1, "units": [{"id": "M1", "goal": "Plant the garden", "files": files,
                              "done": "Planted", "verify": "Inspect the garden", "timebox": 15,
                              "forbid": "No outside edits"}],
    })
    assert result.returncode == 0, result.stderr


def dispatch(root, role, attempt, *options, session="fictional"):
    return run(root, "worker", "dispatch", "M1", "--role", role, "--session", session, "--request-id", attempt,
               "--mode", "native", "--host-role", f"chef:{role}", *options)


def registered_plan(root):
    result = run(root, "plan", "template", "M1")
    assert result.returncode == 0 and "Inspect the garden" in result.stderr
    plan = json.loads(result.stdout)
    assert plan["grade"] == "pytest-pass" and plan["inputs"] == ["garden.txt", "seedlings.txt"]
    assert not plan["commands"] and not plan["environment"]
    plan.update(commands=[{"argv": [sys.executable, "-c", "assert open('garden.txt').read()"]}],
                environment={"fixture": "orchard"})
    saved = run(root, "plan", "save", "M1", "--input", "-", input=plan)
    assert saved.returncode == 0, saved.stderr
    return plan, json.loads(saved.stdout)


def test_compact_dispatch_reuses_approved_scope_and_request_identity(project):
    root, _ = project
    init_unit(root)
    wrong = dispatch(root, "dev-small", "writing", session="someone-else")
    assert wrong.returncode == 1 and "ACTIVE" in wrong.stdout
    outside = dispatch(root, "dev-small", "writing", "--file", "schema.json")
    assert outside.returncode == 1 and "approved unit files" in outside.stdout
    created = dispatch(root, "dev-small", "writing", "--prompt", "Plant in this order")
    assert created.returncode == 0, created.stdout + created.stderr
    job = json.loads(created.stdout)
    assert job["native"] and "Plant the garden" in job["brief"] and "Plant in this order" in job["brief"]
    request = Store().get(job["key"], reconcile=False)["request"]
    assert request["files"] == ["garden.txt", "seedlings.txt"] and request["mission"] == "orchard"
    retry = dispatch(root, "dev-small", "writing", "--prompt", "Plant in this order")
    assert retry.returncode == 0 and json.loads(retry.stdout)["key"] == job["key"]
    assert json.loads(retry.stdout)["created"] is False
    assert dispatch(root, "dev-small", "writing", "--prompt", "Different scope").returncode == 1


def test_plain_scope_only_admits_an_actual_file_and_rejects_opaque_prose(project):
    root, _ = project
    init_unit(root, "garden.txt and related files")
    assert dispatch(root, "dev-small", "writing", "--file", "garden.txt").returncode == 1
    edit = run(root, "unit", "set", "M1", "--files", "garden.txt")
    assert edit.returncode == 0, edit.stderr
    assert dispatch(root, "dev-small", "writing").returncode == 0


def test_ledger_stdin_records_and_audits_current_evidence_hash(project):
    root, folder = project
    init_unit(root)
    evidence = {"schema": 1, "files": [{"path": "garden.txt", "kind": "report", "sha256": "a" * 64}]}
    args = ("ledger", "record", "M1", "--status", "pass", "--grade", "pytest-pass", "--evidence-input", "-")
    ledger = (folder / "ledger.tsv").read_bytes()
    rejected = run(root, *args, input=evidence)
    assert rejected.returncode == 1 and "sha256 differs" in rejected.stderr
    assert (folder / "ledger.tsv").read_bytes() == ledger
    evidence["files"][0].pop("sha256")
    recorded = run(root, *args, input=evidence)
    assert recorded.returncode == 0, recorded.stderr
    assert json.loads(run(root, "ledger", "audit").stdout)["counts"]["checkedFiles"] == 1
    (root / "garden.txt").write_text("changed\n", encoding="utf-8")
    assert json.loads(run(root, "ledger", "audit").stdout)["counts"]["changed"] == 1


def test_registered_plan_is_reusable_with_content_and_revision_checks(project):
    root, _ = project
    init_unit(root)
    plan, first = registered_plan(root)
    repeated = run(root, "plan", "save", "M1", "--input", "-", input=plan)
    assert repeated.returncode == 0 and json.loads(repeated.stdout) == first
    verifier = dispatch(root, "verifier", "verification")
    assert verifier.returncode == 0, verifier.stdout + verifier.stderr
    bound_path = Store().get(json.loads(verifier.stdout)["key"], reconcile=False)["request"]["verification"]
    assert bound_path["path"] == first["path"] and bound_path["sha256"] == first["sha256"]
    assert run(root, "preflight", "M1").returncode == 0
    verified = run(root, "verify", "M1")
    assert verified.returncode == 0, verified.stderr
    assert "재사용" in run(root, "verify", "M1").stdout
    assert run(root, "review-start", "M1").returncode == 0
    assert run(root, "unit", "set", "M1", "--verify", "Inspect again").returncode == 0
    assert json.loads(run(root, "plan", "show", "M1").stdout)["stale"]
    assert run(root, "preflight", "M1").returncode == 1
    plan["environment"] = {"fixture": "second"}
    assert run(root, "plan", "save", "M1", "--input", "-", input=plan).returncode == 1
    replaced = run(root, "plan", "save", "M1", "--input", "-", "--expect-sha256", first["sha256"], input=plan)
    assert replaced.returncode == 0, replaced.stderr
    second = json.loads(replaced.stdout)
    assert second["sha256"] != first["sha256"] and Path(first["path"]).is_file()
    Path(second["path"]).write_text("{}\n", encoding="utf-8")
    assert run(root, "preflight", "M1").returncode == 1
    assert run(root, "preflight", "M1", "--plan", first["path"]).returncode == 0


def test_registered_plan_supplies_review_run_without_an_explicit_path(project):
    root, _ = project
    python = Path(sys.base_prefix) / "python.exe" if os.name == "nt" else Path(sys.executable)
    if not python.is_file():
        pytest.skip("hashable Python executable unavailable")
    init_unit(root)
    plan, _ = registered_plan(root)
    for name in ("scope.json", "findings.json"):
        (root / name).write_text("[]", encoding="utf-8")
    result = run(root, "review-run", "M1", "--runtime", "codex", "--snapshot", digest(snapshot(root, plan["inputs"])),
                 "--purpose", "Inspect garden", "--scope", "scope.json", "--findings", "findings.json",
                 "--", str(python), "-c", "print('Reviewed the garden')", runtime=None)
    assert result.returncode == 0, result.stdout + result.stderr
    review = json.loads(result.stdout)
    assert review["status"] == "completed" and review["result"]["inputsUnchanged"]


def test_named_plan_uses_adapter_commands_and_the_unit_files(project):
    root, folder = project
    adapter = root / ".claude/orchestrator/adapter.json"
    config = json.loads(adapter.read_bytes())
    config["verify"]["planPresets"] = {
        "garden": {"grade": "pytest-pass", "environment": {"target": "orchard"},
                   "commands": [{"argv": [sys.executable, "-c", "assert open('garden.txt').read()"]}]},
    }
    adapter.write_text(json.dumps(config), encoding="utf-8")
    init_unit(root, "garden.txt")
    listed = run(root, "plan", "presets")
    assert json.loads(listed.stdout)["presets"] == [{"name": "garden", "grade": "pytest-pass"}]
    assert run(root, "plan", "save", "M1", "--preset", "unknown").returncode == 1
    result = run(root, "plan", "save", "M1", "--preset", "garden")
    assert result.returncode == 0, result.stderr
    source = json.loads(Path(json.loads(result.stdout)["path"]).read_bytes())
    assert source == {**config["verify"]["planPresets"]["garden"], "inputs": ["garden.txt"]}
    verification = run(root, "verify", "M1")
    assert verification.returncode == 0, verification.stderr
    assert (folder / "verification/M1.latest.json").is_file()


def collect_review(root, job, review):
    result = run(root, "worker", "native-result", "--key", job["key"], "--token", job["token"], "--result", "-",
                 input=review, runtime=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "completed"


def clean_review():
    return {"schema": 1, "report": "Reviewed the garden.", "findings": [], "resolvedIds": [], "suggestions": []}


def test_worker_run_dispatches_collects_records_and_closes_only_confirmed_low(project, monkeypatch, capsys):
    root, folder = project
    init_unit(root)
    registered_plan(root)
    assert run(root, "verify", "M1").returncode == 0
    monkeypatch.chdir(root)
    monkeypatch.setattr(controller.policy.user_config, "read", lambda: {})
    launched = []
    review = clean_review()
    review["findings"] = [{"id": "F1", "kind": "defect", "origin": "new", "severity": "low",
                           "reason": "Missing label", "evidence": "garden.txt:1"}]

    def candidates(_adapter, _cwd, role, **kwargs):
        assert kwargs["native"] is None and kwargs["allowed_runtimes"] == ["codex"]
        return [{"runtime": "codex", "available": True, "roles": [role], "tools": ["Read"]}]

    def backend(runtime, role, prompt, **_kwargs):
        launched.append(role)
        assert runtime == "codex"
        if role == "reviewer-small":
            assert "Read the exact verified change diff" in prompt
        return {"ok": True, "result": json.dumps(review)}

    def launch(store, record, _directory):
        controller.supervise(record["key"], record["token"], store=store)

    monkeypatch.setattr(controller.backends, "candidates", candidates)
    monkeypatch.setattr(controller.backends, "run", backend)
    monkeypatch.setattr(controller, "launch_supervisor", launch)

    def managed(role, attempt, *options, expected=0):
        code = worker_cli.main(["run", "M1", "--role", role, "--request-id", attempt, "--mode", "native",
                                "--session", "fictional", "--runtime", "codex", "--wait-seconds", "5", *options])
        assert code == expected
        return json.loads(capsys.readouterr().out)

    first = managed("code-reviewer", "first", "--record-review")
    assert first["status"] == "completed" and first["execution"] == "supervised"
    assert first["reviewRecord"]["status"] == "recorded"
    proof = json.loads(Path(first["reviewRecord"]["path"]).read_bytes())
    assert proof["findings"][0]["id"] == "F1" and proof["workerJob"] == first["key"]
    assert managed("code-reviewer", "first", "--record-review")["key"] == first["key"]
    directory = Path(first["details"]).parent.parent / "attempts" / first["key"] / first["token"]
    (directory / "review-record.json").unlink()
    assert managed("code-reviewer", "first", "--record-review")["reviewRecord"] == first["reviewRecord"]
    assert "text diff" in managed("reviewer-small", "unchanged", "--close-low", expected=1)["error"]
    assert launched == ["code-reviewer"]

    (root / "garden.txt").write_text("labeled garden\n", encoding="utf-8")
    assert run(root, "verify", "M1").returncode == 0
    with monkeypatch.context() as patch:
        patch.setattr(reviews, "project_path", lambda *_: root / "schema.json")
        stale = managed("reviewer-small", "stale-diff", "--close-low", expected=1)
        assert "complete text diff" in stale["error"]
    assert launched == ["code-reviewer"]
    review = clean_review()
    blocked = managed("reviewer-small", "unresolved", "--close-low", expected=1)
    assert blocked["status"] == "completed" and blocked["reviewRecord"]["status"] == "blocked"
    pointer = folder / "verification/M1.review.latest.json"
    assert json.loads(pointer.read_bytes())["review"] == first["reviewRecord"]["path"]
    review.update(report="The existing label finding is fixed in garden.txt:1.", resolvedIds=["F1"])
    closed = managed("reviewer-small", "close", "--close-low")
    closed_review = json.loads(Path(closed["reviewRecord"]["path"]).read_bytes())
    assert closed_review["status"] == "closed-low" and closed_review["closedLowIds"] == ["F1"]
    diff = Path(closed_review["workerDiff"])
    assert b"+labeled garden" in diff.read_bytes()
    diff.write_text("changed", encoding="utf-8")
    assert run(root, "review-open", "M1").returncode == 1


def test_worker_run_waits_for_a_detached_supervisor_to_record_review(project, tmp_path, monkeypatch, capsys):
    root, folder = project
    init_unit(root)
    registered_plan(root)
    assert run(root, "verify", "M1").returncode == 0
    bootstrap = tmp_path / "supervisor fixture"
    bootstrap.mkdir()
    (bootstrap / "worker.py").write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
        "from chef.workers import backends, cli\n"
        f"backends.run = lambda *args, **kwargs: {{'ok': True, 'review': {clean_review()!r}, 'fixturePid': os.getpid()}}\n"
        "raise SystemExit(cli.main())\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(controller, "PLUGIN_ROOT", bootstrap)
    monkeypatch.setattr(controller.policy.user_config, "read", lambda: {})
    monkeypatch.setattr(controller.backends, "candidates", lambda *_args, **_kwargs: [
        {"runtime": "codex", "available": True, "roles": ["code-reviewer"], "tools": ["Read"]},
    ])
    code = worker_cli.main(["run", "M1", "--role", "code-reviewer", "--request-id", "detached-review",
                            "--mode", "codex", "--session", "fictional", "--runtime", "codex",
                            "--record-review", "--wait-seconds", "20"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["reviewRecord"]["status"] == "recorded", result
    assert Store().get(result["key"])["result"]["fixturePid"] != os.getpid()
    pointer = json.loads((folder / "verification/M1.review.latest.json").read_bytes())
    assert pointer["review"] == result["reviewRecord"]["path"]


def test_review_job_records_first_findings_and_manual_diff_closes_same_id(project):
    root, folder = project
    init_unit(root)
    registered_plan(root)
    assert run(root, "verify", "M1").returncode == 0
    started = dispatch(root, "code-reviewer", "first-review")
    assert started.returncode == 0, started.stdout + started.stderr
    job = json.loads(started.stdout)
    args = ("review", "M1", "--job", job["key"])
    assert run(root, *args, "--session", "fictional").returncode == 1
    result = clean_review()
    result["findings"].append({"id": "F1", "kind": "defect", "origin": "new", "severity": "low",
                               "reason": "Missing label", "evidence": "garden.txt:1"})
    collect_review(root, job, result)
    wrong = run(root, *args, "--session", "someone-else")
    assert wrong.returncode == 1 and "owner session" in wrong.stderr
    pointer = folder / "verification/M1.review.latest.json"
    assert not pointer.exists()
    recorded = run(root, *args, "--session", "fictional")
    assert recorded.returncode == 0, recorded.stderr
    first = pointer.read_bytes()
    assert run(root, *args, "--session", "fictional").returncode == 0
    assert pointer.read_bytes() == first
    first_review = json.loads(Path(json.loads(first)["review"]).read_bytes())
    assert first_review["workerJob"] == job["key"] and first_review["findings"][0]["id"] == "F1"
    assert Path(first_review["evidence"]).read_text(encoding="utf-8") == result["report"]
    assert run(root, *args, "--session", "fictional", "--status", "closed-low").returncode == 1
    (root / "garden.txt").write_text("labeled garden\n", encoding="utf-8")
    assert run(root, *args, "--session", "fictional").returncode == 1
    scope = run(root, "review-scope", "M1")
    assert scope.returncode == 0 and "garden.txt" in scope.stdout
    assert run(root, "verify", "M1").returncode == 0
    receipt = json.loads((folder / "verification/M1.latest.json").read_bytes())["receipt"]
    diff = root / "reviewed-diff.md"
    diff.write_text("Confirmed label in garden.txt:1.\n", encoding="utf-8")
    closed = run(root, "review", "M1", "--receipt", receipt, "--evidence", str(diff), "--reviewer", "coordinator",
                 "--kind", "unit", "--status", "closed-low", "--resolved-id", "F1")
    assert closed.returncode == 0, closed.stderr
    current = json.loads(Path(json.loads(pointer.read_bytes())["review"]).read_bytes())
    assert current["closedLowIds"] == ["F1"] and current["round"] == 2


def test_review_job_rejects_superseded_receipt_and_unstructured_result(project):
    root, folder = project
    init_unit(root)
    registered_plan(root)
    assert run(root, "verify", "M1").returncode == 0
    started = dispatch(root, "reviewer-small", "review")
    assert started.returncode == 0, started.stdout + started.stderr
    job = json.loads(started.stdout)
    invalid = run(root, "worker", "native-result", "--key", job["key"], "--token", job["token"], "--result", "-",
                  input={"ok": True, "result": "No issues."}, runtime=None)
    assert invalid.returncode == 1 and "structured JSON" in invalid.stdout
    collect_review(root, job, clean_review())
    assert run(root, "verify", "M1", "--force").returncode == 0
    rejected = run(root, "review", "M1", "--job", job["key"], "--session", "fictional")
    assert rejected.returncode == 1 and "마지막 검증 기록" in rejected.stderr
    assert not (folder / "verification/M1.review.latest.json").exists()


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("action", ["worker dispatch M1 --role code-reviewer", "worker run M1 --role code-reviewer",
                                    "review M1 --job candidate"])
def test_host_guard_checks_new_commands_against_actual_host_identity(project, runtime, action):
    root, folder = project
    mission.write_active(str(folder.parent), mission.Active("orchard", "actual-session", "original", runtime=runtime))
    command = f'python "{PLUGIN_ROOT / "chef_cli.py"}" {action}'
    payload = {"cwd": str(root), "han_runtime": runtime, "session_id": "actual-session", "tool_name": "PowerShell"}
    valid = f"{command} --runtime={runtime} --session actual-session"
    assert not mission.decide({**payload, "tool_input": {"command": valid}}).deny
    wrong_session = valid.replace("actual-session", "different-session")
    assert "--session" in mission.decide({**payload, "tool_input": {"command": wrong_session}}).deny
    wrong_runtime = valid.replace(f"--runtime={runtime}", "--runtime=foreign")
    assert "--runtime" in mission.decide({**payload, "tool_input": {"command": wrong_runtime}}).deny


def test_typed_schema_is_checked_and_tampering_cannot_be_shared(project):
    root, folder = project
    schema = tree_block()
    created = run(root, "document", "create", "--kind", "schema", "--name", "tree", "--input", "-", input=schema)
    assert created.returncode == 0 and "verified" in created.stdout, created.stderr
    name = "details/tree.md"
    assert run(root, "document", "share", name).returncode == 0
    (folder / name).write_text((folder / name).read_text(encoding="utf-8") + "| Key | str |\n", encoding="utf-8")
    assert run(root, "document", "share", name).returncode == 1
    schema["fields"].append({"name": "Key", "type": "str"})
    rejected = run(root, "document", "create", "--kind", "schema", "--name", "mixed", "--input", "-", input=schema)
    assert rejected.returncode == 1 and "does not match" in rejected.stderr
    assert not (folder / "details/mixed.md").exists()


@pytest.mark.parametrize("area", ["blocks", "detail_blocks"])
@pytest.mark.parametrize("untyped", ["fields", "table"])
def test_new_confirm_rejects_generic_schema_tables_before_publication(project, area, untyped):
    root, folder = project
    record = json.loads((PLUGIN_ROOT / "skills/mission/references/templates/design-confirm.json").read_text(encoding="utf-8"))
    record["blocks"] = [tree_block()]
    improper = {"id": "mixed", "type": untyped, "title": "Mixed definitions"}
    if untyped == "table":
        improper.update(columns=["Store", "Attribute", "Format"],
                        rows=[["Orchard DB", "Id", "int"], ["Catalog Proto", "Key", "str"]])
    else:
        improper["fields"] = [{"name": "Id", "type": "int"}, {"name": "Key", "type": "str"}]
    record.setdefault(area, []).append(improper)
    path = root / "draft.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = run(root, "question", "publish", "--input", str(path))
    assert result.returncode == 1 and "use a schema-table" in result.stderr
    assert not (folder / "confirm-A.md").exists()
    record[area].remove(improper)
    if not record[area]:
        record.pop(area)
    path.write_text(json.dumps(record), encoding="utf-8")
    result = run(root, "question", "publish", "--input", str(path))
    assert result.returncode == 0 and "Schema db Tree: verified" in result.stdout, result.stderr
    assert "`DB Tree`" in (folder / "confirm-A.md").read_text(encoding="utf-8")
