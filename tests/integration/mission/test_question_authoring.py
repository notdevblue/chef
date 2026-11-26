"""Bounded authoring through normal draft, publish, answer and render entry points."""

import copy
import json
import re
import os
import subprocess
import sys

import pytest

from chef.questions import records as questions
from chef.questions.authored import render_authored_block
from tests.paths import PLUGIN_ROOT


def draft():
    return {
        "schema": 2, "id": "confirm-A2", "revision": 1,
        "title": "Reading list design", "intro": "Approve the reading list contract.",
        "items": [{"id": "storage", "title": "Save reading state?", "body": "Choose the storage scope.",
                   "status": "pending", "answer": "", "options": [
                       {"id": "account", "label": "Account", "description": "Share state.", "impact": "All devices use it."},
                       {"id": "device", "label": "Device", "description": "Keep local state."}]}],
        "body": "", "blocks": [
            {"id": "rules", "type": "rules", "title": "Validation", "items": [
                {"id": "unique", "label": "Unique ID", "result": "Reject duplicates."},
                {"id": "root", "label": "Root", "when": "Parent is blank", "result": "Create a root."}]},
            {"id": "save", "type": "steps", "title": "Save", "items": [
                {"id": "read", "label": "Read", "text": "Load the list."},
                {"id": "write", "label": "Write", "text": "Save the list.", "failure": "Roll back."}]},
            {"id": "scope", "type": "list", "title": "Scope", "items": [
                {"id": "books", "label": "Books", "text": "Apply to saved books."}]},
            {"id": "book", "type": "fields", "title": "Book", "fields": [
                {"name": "Id", "type": "string", "description": "Stable ID."}]},
            {"id": "cost", "type": "table", "title": "Costs", "columns": ["Action", "Reads"],
             "rows": [["Open", "1"]], "row_ids": ["open"]}],
        "approval": {"status": "pending", "reason": "Approve the complete contract."}, "answers": [],
    }


def snapshot(folder):
    return {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def test_structured_input_publishes_and_renders_separate_choices_and_steps(tmp_path):
    record = draft()
    before = copy.deepcopy(record)
    assert questions.preflight_document(tmp_path, record) == record
    assert snapshot(tmp_path) == {}
    questions.publish_document(tmp_path, record)
    text = (tmp_path / "confirm-A2.md").read_text(encoding="utf-8")
    assert 'id="rules.unique"' in text and 'id="cost.open"' in text
    assert '1. <a id="save.read"' in text and '2. <a id="save.write"' in text
    assert "Share state.  \n  영향" in text
    assert not questions.inspect_document(tmp_path, "confirm-A2.md", text)[1]
    assert record == before


@pytest.mark.parametrize("mutate,path", [
    (lambda d: d.update(title="x" * 81), "title"),
    (lambda d: d.update(body="x"), "body"),
    (lambda d: d["blocks"][0]["items"][0].update(result="short\n" * 30), "result"),
    (lambda d: d["blocks"][0]["items"][0].update(result="<div>hidden</div>"), "result"),
    (lambda d: d["blocks"][0]["items"][0].update(result="[hidden](https://example.org)"), "result"),
    (lambda d: d["blocks"][0].update(type="paragraph", text="long"), "sentences"),
])
def test_all_authored_prose_paths_reject_oversize_or_embedded_blocks_without_writes(tmp_path, mutate, path):
    record = draft()
    mutate(record)
    for operation in (questions.validate_document, questions.render_document):
        with pytest.raises(questions.QuestionRecordError, match=re.escape(path)):
            operation(record)
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError):
            operation(tmp_path, record)
        assert snapshot(tmp_path) == {}


def test_batch_reports_all_errors_and_repairs_one_entry_without_rewriting_block(tmp_path):
    record = draft()
    changes = {"set": {"intro": "<div>invalid</div>"}, "upsert_entries": [
        {"block": "rules", "entry": {"id": "unique", "label": "Unique", "result": "<div>invalid</div>"}}]}
    with pytest.raises(questions.QuestionRecordError) as error:
        questions.patch_document(record, changes)
    assert "intro" in str(error.value) and "blocks[0].items[0].result" in str(error.value)
    assert record == draft()
    changes["set"]["intro"] = "Revised."
    changes["upsert_entries"][0]["entry"]["result"] = "Reject repeated IDs."
    patched = questions.patch_document(record, changes)
    assert patched["blocks"][0]["items"][1] == record["blocks"][0]["items"][1]
    questions.publish_document(tmp_path, patched)


@pytest.mark.parametrize("mode", ["all", "any"])
def test_conditions_exceptions_priority_and_references(mode):
    record = draft()
    rules = record["blocks"][0]
    rules["items"][0]["conditions"] = {"mode": mode, "items": ["First condition", "Second condition"]}
    rules["items"][1]["exception_of"] = "rules.unique"
    rules["items"][1]["refs"] = ["cost.open", "book.Id"]
    rules["priority"] = ["root", "unique"]
    text = questions.render_document(record)
    assert text.index('id="rules.root"') < text.index('id="rules.unique"')
    assert "(#cost.open)" in text and "(#book.Id)" in text
    rules["items"][0]["exception_of"] = "rules.root"
    with pytest.raises(questions.QuestionRecordError, match="순환"):
        questions.validate_document(record)


@pytest.mark.parametrize("value", ["missing", "rules.unique", "book.Id", []])
def test_invalid_exception_target_or_cycle_is_rejected(value):
    record = draft()
    record["blocks"][0]["items"][0].update(when="Special case", exception_of=value)
    with pytest.raises(questions.QuestionRecordError):
        questions.validate_document(record)


def test_long_user_answer_and_conditional_approval_are_preserved_and_linked(tmp_path):
    questions.publish_document(tmp_path, draft())
    raw = "A long answer.\n" * 300
    payload = {"schema": 1, "id": "reply", "revision": 1, "at": "2041-02-08",
               "text": raw, "updates": [{"id": "storage", "status": "answered", "answer": raw, "selected": "account"}],
               "approval": {"status": "pending", "reason": raw}}
    saved = questions.answer_document(tmp_path, "confirm-A2.md", payload)
    assert saved["answers"] == [payload] and saved["approval"]["reason"] == raw
    text = questions.render_document(saved)
    assert raw not in text and "현재 승인 조건 원문" in text and "승인 대기" in text
    assert raw in questions.history_path(tmp_path, "confirm-A2.md").read_text(encoding="utf-8")
    assert questions.answer_document(tmp_path, "confirm-A2.md", payload) == saved
    changed = copy.deepcopy(saved)
    changed["revision"] += 1
    changed["approval"]["reason"] = "Changed " + raw
    with pytest.raises(questions.QuestionRecordError):
        questions.publish_document(tmp_path, changed, 1)


def test_legacy_reads_answers_and_explicit_editorial_upgrade_with_history(tmp_path):
    legacy = draft()
    legacy["schema"] = 1
    legacy["blocks"] = [{"id": "scope", "type": "paragraph", "text": "Old prose. " * 100}]
    (tmp_path / "questions").mkdir()
    questions.source_path(tmp_path, "confirm-A2.md").write_text(json.dumps(legacy), encoding="utf-8")
    questions.render_saved_document(tmp_path, "confirm-A2.md")
    assert questions.read_document(tmp_path, "confirm-A2.md") == legacy
    upgraded = questions.patch_document(legacy, {"set": {"schema": 2, "revision": 2},
        "upsert_blocks": draft()["blocks"][:3]}, editorial=True)
    questions.publish_document(tmp_path, upgraded, 1, editorial=True)
    assert json.loads((tmp_path / "questions/history/confirm-A2/1.json").read_text(encoding="utf-8")) == legacy
    downgraded = copy.deepcopy(legacy)
    downgraded["revision"] = 3
    with pytest.raises(questions.QuestionRecordError, match="되돌릴"):
        questions.publish_document(tmp_path, downgraded, 2)
    with pytest.raises(questions.QuestionRecordError, match="되돌릴"):
        questions.patch_document(upgraded, {"set": {"schema": 1}})


def test_new_confirmations_cannot_use_legacy_schema(tmp_path):
    legacy = draft()
    legacy["schema"] = 1
    legacy["blocks"] = [{"id": "scope", "type": "paragraph", "text": "Old prose."}]
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="schema 2"):
            operation(tmp_path, legacy)
    assert snapshot(tmp_path) == {}


def test_initial_document_cannot_forge_answer_evidence_to_bypass_authoring_limits(tmp_path):
    record = draft()
    raw = "long answer" * 100
    record["approval"] = {"status": "pending", "reason": raw}
    record["answers"] = [{"schema": 1, "id": "forged", "revision": 1, "at": "2041-02-08",
                          "text": raw, "updates": [], "approval": copy.deepcopy(record["approval"])}]
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="answer"):
            operation(tmp_path, record)
    assert snapshot(tmp_path) == {}


@pytest.mark.parametrize("format,value", [("identifier", "A" * 500), ("formula", "a + b * c"),
    ("code", "if (x < 2) {\n  return x;\n}"), ("link", "https://example.org/" + "a" * 500)])
def test_technical_literals_preserve_value_and_escape_markup(format, value):
    record = draft()
    block = {"id": "literal", "type": "literal", "title": "Reference", "format": format, "value": value}
    record["blocks"].append(block)
    questions.validate_document(record)
    rendered = render_authored_block(block)
    assert record["blocks"][-1]["value"] == value
    assert "< 2" not in rendered
    if format != "link":
        assert "pre-wrap" in rendered


@pytest.mark.parametrize("value", ["javascript:alert(1)", "//example.org", "https://", "https://example.org/\nnext"])
def test_unsafe_literal_links_are_rejected(value):
    record = draft()
    record["blocks"].append({"id": "link", "type": "literal", "title": "Link", "format": "link", "value": value})
    with pytest.raises(questions.QuestionRecordError):
        questions.validate_document(record)


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_runtime_cli_previews_the_same_template_and_batch_errors(tmp_path, runtime):
    adapter = tmp_path / ".claude/orchestrator"
    adapter.mkdir(parents=True)
    (adapter / "adapter.json").write_text(json.dumps({"schema": 1, "paths": {"stateDir": "state"}}), encoding="utf-8")
    source = tmp_path / "draft.json"
    source.write_text(json.dumps(draft()), encoding="utf-8")
    environment = dict(os.environ, HAN_ADAPTERS_DIR=str(tmp_path / "no-registry"), PYTHONUTF8="1")
    for name in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        environment.pop(name, None)
    script = PLUGIN_ROOT / "skills/mission/scripts/mission_state.py"
    if runtime == "codex":
        script = PLUGIN_ROOT / "codex/skills/chef-mission/scripts/mission_state.py"
    argv = [sys.executable, str(script), "--project", str(tmp_path), "--runtime", runtime,
            "--mission", "fictional", "question", "preview", "--input", str(source)]
    result = subprocess.run(argv, cwd=tmp_path, env=environment, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0 and result.stderr == "", result.stderr
    assert 'id="rules.unique"' in result.stdout and "All devices use it." in result.stdout
    bad = draft()
    bad["intro"] = "<div>invalid</div>"
    bad["blocks"][0]["items"][0]["result"] = "<div>invalid</div>"
    source.write_text(json.dumps(bad), encoding="utf-8")
    result = subprocess.run(argv, cwd=tmp_path, env=environment, capture_output=True, encoding="utf-8", timeout=30)
    assert result.stdout == "" and "intro" in result.stderr and "blocks[0].items[0].result" in result.stderr
    assert not (tmp_path / "state").exists()


def test_field_types_keep_generic_syntax_and_protected_tables_survive_editorial_updates(tmp_path):
    record = draft()
    record["blocks"][3]["fields"][0]["type"] = "List<string>"
    questions.publish_document(tmp_path, record)
    assert "List&lt;string&gt;" in questions.render_document(record)
    changed = questions.patch_document(record, {"set": {"revision": 2}, "upsert_entries": [
        {"block": "rules", "entry": {"id": "unique", "label": "Unique ID", "result": "Reject duplicate IDs."}}]}, editorial=True)
    questions.publish_document(tmp_path, changed, 1, editorial=True)
    stale = copy.deepcopy(changed)
    stale["revision"] = 3
    before = snapshot(tmp_path)
    with pytest.raises(questions.QuestionRecordError, match="expected_revision"):
        questions.publish_document(tmp_path, stale, 1)
    assert snapshot(tmp_path) == before
    stale["blocks"][3]["fields"][0]["type"] = "int"
    with pytest.raises(questions.QuestionRecordError, match="editorial"):
        questions.publish_document(tmp_path, stale, 2, editorial=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("template", ["question.json", "question-structured.json", "design-confirm.json"])
def test_shipped_templates_are_valid_without_rewriting(template):
    source = PLUGIN_ROOT / "skills/mission/references/templates" / template
    record = json.loads(source.read_text(encoding="utf-8"))
    assert record["schema"] == 2
    assert questions.validate_document(record) == record
    assert questions.render_document(record)
