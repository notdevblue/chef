"""Exercise the bundled entry point against an isolated fictional project."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from chef.guards import mission
from chef.mission.contracts import StateError
from chef.mission.documents import share_documents, shared_documents
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


def run(project, *args, input=None):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(PLUGIN_ROOT / "chef_cli.py"), *args, "--runtime", "codex"],
        cwd=project, env={**os.environ, "PYTHONUTF8": "1"}, input=json.dumps(input) if input else None,
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
    assert run(root, "document", "share", "report.md").returncode == 0
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
        improper.update(columns=["Column", "Type"], rows=[["Id", "int"], ["Key", "str"]])
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
