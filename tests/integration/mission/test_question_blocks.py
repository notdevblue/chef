from pathlib import Path

from tests.paths import PLUGIN_ROOT

"""Guard structured draft publication against wrong answers and partial saves."""

import copy
import hashlib
import json
import os
import sys

import pytest
from chef.questions import records as questions


def draft():
    return {
        "schema": 1, "id": "ask-palette", "revision": 1, "title": "Palette", "intro": "Choose a palette.",
        "items": [{"id": "color", "title": "Color", "body": "Choose one.", "status": "pending", "answer": "",
                   "options": [{"id": "blue", "label": "Blue", "description": "Cool", "impact": "Low contrast"},
                               {"id": "amber", "label": "Amber"}]}],
        "body": "", "blocks": [
            {"id": "scope", "type": "paragraph", "text": "A fictional palette."},
            {"id": "values", "type": "table", "title": "Values", "columns": ["Name", "Note"], "rows": [["A|B", "line 1\nline 2"]]},
            {"id": "palette", "type": "fields", "fields": [{"name": "Id", "type": "string", "key": "primary"},
                                                              {"name": "Parent", "type": "string", "ref": "palette.Id", "description": "Optional"}]},
        ], "detail_blocks": [{"id": "evidence", "type": "paragraph", "text": "Fictional display measurements."}],
        "approval": {"status": "none", "reason": ""}, "answers": [],
    }


def reply(selected="blue"):
    update = {"id": "color", "status": "answered", "answer": "Use the cool palette."}
    if selected is not None:
        update["selected"] = selected
    return {"schema": 1, "id": "reply-color", "revision": 1, "at": "2041-02-08", "text": "Blue, please.\nKeep this exact text.", "updates": [update]}


def files(folder):
    return {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}


def test_structured_blocks_and_explicit_selected_render_without_body_rewriting(tmp_path):
    original = questions.publish_document(tmp_path, draft())
    current = (tmp_path / "ask-palette.md").read_text(encoding="utf-8")
    assert "| A\\|B | line 1<br>line 2 |" in current
    assert "| Parent | string |  | palette.Id | Optional |" in current
    assert "### 1. Color" in current
    assert "- Blue  \n  설명 · Cool  \n  영향 · Low contrast" in current
    assert "- Amber" in current
    assert "| id | 선택 |" not in current
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply())
    rendered = questions.render_document(saved)
    assert "## 현재 선택\n\n### Color\n\n- Blue  \n  설명 · Cool  \n  영향 · Low contrast" in rendered
    assert "| 문항 | 선택 id |" not in rendered
    assert "amber" not in rendered and saved["blocks"] == original["blocks"] and saved["body"] == ""
    assert reply()["text"] not in rendered and saved["answers"] == [reply()]
    assert reply()["text"] in (tmp_path / "details/history/ask-palette.md").read_text(encoding="utf-8")
    assert not questions.inspect_document(tmp_path, "ask-palette.md", rendered)[1]


def test_current_render_elides_exact_option_repeat_and_retains_distinct_context():
    record = draft()
    item = record["items"][0]
    item["options"][0].update(label="권장: Blue", description="Cool\ncolor", impact="Cool color")
    item["body"] = "- 권장: Blue — Cool color\n\nUse the alternate palette outdoors."
    current = questions.render_document(record)
    assert current.count("- 권장: Blue — Cool color") == 1
    assert "Use the alternate palette outdoors." in current
    assert "- Amber" in current
    assert "본문과 options" in questions.decision_review_notes(record)[0]


def test_existing_option_table_is_valid_until_current_document_is_regenerated(tmp_path):
    questions.publish_document(tmp_path, draft())
    record = questions.answer_document(tmp_path, "ask-palette.md", reply())
    name = "ask-palette.md"
    original = files(tmp_path)
    old = questions.render_document(record, tabular_choices=True)
    assert "| Color | blue | Blue | Cool | Low contrast |" in old
    (tmp_path / name).write_bytes(old.encode("utf-8"))
    assert not questions.inspect_document(tmp_path, name, old)[1]
    assert questions.inspect_document(tmp_path, name, old.replace("Low contrast", "High contrast"))[1]
    questions.render_saved_document(tmp_path, name)
    assert files(tmp_path) == original


@pytest.mark.parametrize("change", [
    lambda d: d.update(body="Duplicated contract"),
    lambda d: d.update(blocks={}),
    lambda d: d["blocks"][0].update(type="script"),
    lambda d: d["blocks"][0].update(path="outside.md"),
    lambda d: d["blocks"][0].update(id="../outside"),
    lambda d: d["detail_blocks"][0].update(id="scope"),
    lambda d: d["blocks"][1].update(rows=[["wrong width"]]),
    lambda d: d["blocks"][1].update(rows=[["Name", 42]]),
    lambda d: d["blocks"][2]["fields"][1].update(ref="missing.Id"),
    lambda d: d["blocks"][2]["fields"][1].update(name="Id"),
    lambda d: d["items"][0]["options"][1].update(id="blue"),
    lambda d: d["items"][0]["options"][0].update(label=1),
    lambda d: d["items"][0]["options"][0].update(unknown="hidden"),
    lambda d: d["items"][0].update(selected="blue"),
    lambda d: d["items"][0].update(status="answered", answer="Chosen", selected="missing"),
])
def test_invalid_structured_values_never_create_records(tmp_path, change):
    record = draft()
    change(record)
    with pytest.raises(questions.QuestionRecordError):
        questions.publish_document(tmp_path, record)
    assert files(tmp_path) == {}


def test_invalid_selection_is_rejected_and_free_answers_never_infer_it(tmp_path):
    questions.publish_document(tmp_path, draft())
    before = files(tmp_path)
    for payload in (reply("missing"), {**reply(), "updates": [{**reply()["updates"][0], "status": "withdrawn"}]}):
        with pytest.raises(questions.QuestionRecordError):
            questions.answer_document(tmp_path, "ask-palette.md", payload)
        assert files(tmp_path) == before
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply(None))
    assert "selected" not in saved["items"][0] and "## 현재 선택" not in questions.render_document(saved)


def test_initial_publication_cannot_install_selected_without_an_answer_operation(tmp_path):
    record = draft()
    record["items"][0].update(status="answered", answer="Preselected", selected="blue")
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="selected.*answer"):
            operation(tmp_path, record)
        assert list(tmp_path.iterdir()) == []
    record["items"][0].pop("selected")
    saved = questions.publish_document(tmp_path, record)
    assert saved["items"][0]["status"] == "answered" and saved["answers"] == []
    selected = questions.answer_document(tmp_path, "ask-palette.md", reply())
    before = files(tmp_path)
    assert questions.preflight_document(tmp_path, selected) == selected
    assert questions.publish_document(tmp_path, selected) == selected
    assert questions.answer_document(tmp_path, "ask-palette.md", reply()) == selected
    assert files(tmp_path) == before and selected["items"][0]["selected"] == "blue"


@pytest.mark.parametrize("field", ["title", "options"])
def test_changed_withdrawn_definition_requires_explicit_reopen_and_keeps_history(tmp_path, field):
    questions.publish_document(tmp_path, draft())
    payload = reply(None)
    payload["updates"][0].update(status="withdrawn", answer="Exclude the original color choice.")
    saved = questions.answer_document(tmp_path, "ask-palette.md", payload)
    definition = copy.deepcopy({key: value for key, value in saved["items"][0].items() if key not in ("status", "answer")})
    if field == "title":
        definition["title"] = "New color decision"
    else:
        definition["options"][0]["label"] = "New meaning"
    changes = {"set": {"revision": 2}, "upsert_items": [definition]}
    with pytest.raises(questions.QuestionRecordError, match="reopen_items"):
        questions.patch_document(saved, changes)
    changed = copy.deepcopy(saved)
    changed["revision"] = 2
    changed["items"][0].update(definition)
    before = files(tmp_path)
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="pending.*answer"):
            operation(tmp_path, changed, 1)
        assert files(tmp_path) == before
    changes["reopen_items"] = ["color"]
    reopened = questions.patch_document(saved, changes)
    questions.publish_document(tmp_path, reopened, 1)
    assert reopened["items"][0]["status"] == "pending" and reopened["items"][0]["answer"] == ""
    assert "selected" not in reopened["items"][0] and reopened["answers"] == [payload]
    assert questions.summarize_document(reopened).pending


@pytest.mark.parametrize("outcome", ["withdrawn", "selected"])
def test_revision_cannot_hide_answer_changes_behind_an_item_definition_edit(tmp_path, outcome):
    questions.publish_document(tmp_path, draft())
    payload = reply()
    if outcome == "withdrawn":
        payload = reply(None)
        payload["updates"][0].update(status="withdrawn", answer="Original withdrawal.")
    saved = questions.answer_document(tmp_path, "ask-palette.md", payload)
    changed = copy.deepcopy(saved)
    changed["revision"] = 2
    changed["items"][0]["title"] = "Different decision"
    if outcome == "withdrawn":
        changed["items"][0]["answer"] = "Different withdrawal."
    else:
        changed["items"][0]["selected"] = "amber"
    before = files(tmp_path)
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="pending.*answer"):
            operation(tmp_path, changed, 1)
        assert files(tmp_path) == before


@pytest.mark.parametrize("field", ["title", "options"])
def test_pending_definition_patch_requires_explicit_reopen_without_mutating_inputs(field):
    record = draft()
    definition = copy.deepcopy({key: value for key, value in record["items"][0].items() if key not in ("status", "answer")})
    if field == "title":
        definition["title"] = "New pending question"
    else:
        definition["options"][0]["label"] = "New pending option"
    changes = {"upsert_items": [definition]}
    original = copy.deepcopy(record)
    original_changes = copy.deepcopy(changes)
    with pytest.raises(questions.QuestionRecordError, match="reopen_items"):
        questions.patch_document(record, changes)
    assert record == original and changes == original_changes
    changes["reopen_items"] = ["color"]
    explicit_changes = copy.deepcopy(changes)
    patched = questions.patch_document(record, changes)
    assert record == original and changes == explicit_changes
    assert patched["items"][0] == {**definition, "status": "pending", "answer": ""}


@pytest.mark.parametrize("new_item", [False, True])
def test_same_definition_upsert_and_new_item_do_not_require_reopen(new_item):
    record = draft()
    original = copy.deepcopy(record)
    definition = copy.deepcopy({key: value for key, value in record["items"][0].items() if key not in ("status", "answer")})
    if new_item:
        definition["id"] = "new-color"
    changes = {"upsert_items": [definition]}
    original_changes = copy.deepcopy(changes)
    patched = questions.patch_document(record, changes)
    assert record == original and changes == original_changes
    if new_item:
        assert len(patched["items"]) == 2 and patched["items"][-1] == {**definition, "status": "pending", "answer": ""}
    else:
        assert patched == record


def test_patch_editorial_keeps_selected_and_reopen_requires_explicit_operation(tmp_path):
    questions.publish_document(tmp_path, draft())
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply())
    definition = copy.deepcopy({key: value for key, value in saved["items"][0].items() if key not in ("status", "answer", "selected")})
    definition["body"] = "Choose a color."
    changes = {"set": {"revision": 2}, "upsert_items": [definition]}
    original = copy.deepcopy(saved)
    original_changes = copy.deepcopy(changes)
    with pytest.raises(questions.QuestionRecordError, match="reopen_items"):
        questions.patch_document(saved, changes)
    patched = questions.patch_document(saved, changes, editorial=True)
    assert saved == original and changes == original_changes
    assert patched["items"][0]["selected"] == "blue" and patched["answers"] == saved["answers"]
    with pytest.raises(questions.QuestionRecordError, match="pending"):
        questions.preflight_document(tmp_path, patched, 1)
    questions.publish_document(tmp_path, patched, 1, editorial=True)
    definition["options"][0]["label"] = "Different meaning"
    with pytest.raises(questions.QuestionRecordError, match="options"):
        questions.patch_document(patched, {"set": {"revision": 3}, "upsert_items": [definition]}, editorial=True)
    reopened = questions.patch_document(patched, {"set": {"revision": 3}, "upsert_items": [definition], "reopen_items": ["color"]})
    assert reopened["items"][0]["status"] == "pending" and reopened["items"][0]["answer"] == ""
    assert "selected" not in reopened["items"][0] and reopened["answers"] == saved["answers"]
    questions.publish_document(tmp_path, reopened, 2)
    assert questions.read_document(tmp_path, "ask-palette.md")["answers"] == [reply()]


@pytest.mark.parametrize("operation", ["options", "reopen", "new-item", "remove-item", "state", "approval", "table"])
def test_editorial_patch_reuses_publication_protections_and_keeps_failed_inputs(tmp_path, operation):
    questions.publish_document(tmp_path, draft())
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply())
    definition = copy.deepcopy({key: value for key, value in saved["items"][0].items() if key not in ("status", "answer", "selected")})
    if operation == "options":
        definition["options"][0]["label"] = "Different meaning"
        changes = {"upsert_items": [definition]}
    elif operation == "reopen":
        changes = {"reopen_items": ["color"]}
    elif operation == "new-item":
        definition["id"] = "new-color"
        changes = {"upsert_items": [definition]}
    elif operation == "remove-item":
        changes = {"remove_items": ["color"]}
    elif operation == "state":
        definition["status"] = "pending"
        changes = {"upsert_items": [definition]}
    elif operation == "approval":
        changes = {"request_approval": {"reason": "A new approval request."}}
    else:
        changes = {"upsert_blocks": [{"id": "values", "type": "table", "columns": ["Value"], "rows": [["Changed contract"]]}]}
    original = copy.deepcopy(saved)
    original_changes = copy.deepcopy(changes)
    before_files = files(tmp_path)
    with pytest.raises(questions.QuestionRecordError):
        questions.patch_document(saved, changes, editorial=True)
    assert saved == original and changes == original_changes and files(tmp_path) == before_files


@pytest.mark.parametrize("changes", [
    {"set": {"answers": []}}, {"set": {"approval": {"status": "complete", "reason": ""}}},
    {"set": {"id": "ask-other"}}, {"set": {"legacy_markdown": "overwrite"}},
    {"upsert_items": [{"id": "color", "title": "Color", "body": "", "status": "answered", "answer": "Changed"}]},
    {"upsert_items": [{"id": "color", "title": "Color", "body": "", "selected": "amber"}]},
    {"request_approval": {"reason": "", "status": "complete"}}, {"request_approval": {"reason": ""}},
    {"remove_blocks": ["missing"]}, {"reopen_items": ["missing"]},
    {"upsert_blocks": [{"id": "scope", "type": "paragraph", "text": "Changed"}], "remove_blocks": ["scope"]},
])
def test_patch_cannot_bypass_protected_fields_or_execute_unknown_operations(changes):
    record = draft()
    before = copy.deepcopy(record)
    with pytest.raises(questions.QuestionRecordError):
        questions.patch_document(record, changes)
    assert record == before


def test_patch_uses_explicit_revision_and_request_approval_without_mutating_input(tmp_path):
    questions.publish_document(tmp_path, draft())
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply())
    changes = {"set": {"revision": 2}, "upsert_blocks": [{"id": "scope", "type": "paragraph", "text": "A larger scope."}]}
    patched = questions.patch_document(saved, changes)
    with pytest.raises(questions.QuestionRecordError, match="approval"):
        questions.preflight_document(tmp_path, patched, 1)
    changes["request_approval"] = {"reason": "Confirm the larger scope separately."}
    requested = questions.patch_document(saved, changes)
    assert requested["approval"] == {"status": "pending", "reason": changes["request_approval"]["reason"]}
    assert saved["revision"] == 1 and saved["approval"]["status"] == "none"
    assert questions.patch_document(saved, {}) == saved
    assert questions.preflight_document(tmp_path, requested, 1) == requested
    assert requested["answers"] == saved["answers"]


@pytest.mark.parametrize("field", ["blocks", "detail_blocks"])
def test_structured_table_changes_cannot_use_editorial_to_keep_old_approval(tmp_path, field):
    record = draft()
    record[field].append({"id": "contract", "type": "table", "columns": ["Value"], "rows": [["before"]]})
    questions.publish_document(tmp_path, record)
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply())
    revised = copy.deepcopy(saved)
    revised["revision"] = 2
    revised[field][-1]["rows"] = [["after"]]
    for editorial, message in ((True, "table/fields"), (False, "approval")):
        with pytest.raises(questions.QuestionRecordError, match=message):
            questions.preflight_document(tmp_path, revised, 1, editorial=editorial)
    revised["approval"] = {"status": "pending", "reason": "Confirm changed value."}
    questions.publish_document(tmp_path, revised, 1)


def test_changed_option_id_requires_reopen_and_old_selected_remains_only_in_history(tmp_path):
    questions.publish_document(tmp_path, draft())
    saved = questions.answer_document(tmp_path, "ask-palette.md", reply())
    definition = copy.deepcopy({key: value for key, value in saved["items"][0].items() if key not in ("status", "answer", "selected")})
    definition["options"][0]["id"] = "green"
    changes = {"set": {"revision": 2}, "upsert_items": [definition]}
    with pytest.raises(questions.QuestionRecordError, match="reopen_items"):
        questions.patch_document(saved, changes)
    changes["reopen_items"] = ["color"]
    reopened = questions.patch_document(saved, changes)
    questions.publish_document(tmp_path, reopened, 1)
    assert reopened["answers"][0]["updates"][0]["selected"] == "blue"
    assert "selected" not in reopened["items"][0]


def test_preflight_matches_publish_validation_without_any_writes(tmp_path):
    record = draft()
    before = files(tmp_path)
    assert questions.preflight_document(tmp_path, record) == record
    assert list(tmp_path.iterdir()) == [] and files(tmp_path) == before
    questions.publish_document(tmp_path, record)
    revised = questions.patch_document(record, {"set": {"revision": 2, "title": "Palette wording"}})
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    assert questions.preflight_document(tmp_path, revised, 1, editorial=True) == revised
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="expected_revision"):
            operation(tmp_path, revised, 99, editorial=True)
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    questions.publish_document(tmp_path, revised, 1, editorial=True)


def test_render_and_identical_republish_preserve_original_json_bytes(tmp_path):
    saved = questions.publish_document(tmp_path, draft())
    source = tmp_path / "questions/ask-palette.json"
    original = b"\xef\xbb\xbf" + json.dumps(saved, ensure_ascii=False).encode("utf-8") + b"\r\n"
    source.write_bytes(original)
    questions.render_saved_document(tmp_path, "ask-palette.md")
    questions.publish_document(tmp_path, saved)
    assert source.read_bytes() == original


@pytest.mark.parametrize("content", ["Handwritten detail", "<!-- chef:question-detail:ask-palette:v1 -->\nForged ownership"])
def test_manual_detail_never_overwritten_even_with_generated_marker(tmp_path, content):
    detail = tmp_path / "details/ask-palette.md"
    detail.parent.mkdir()
    detail.write_text(content, encoding="utf-8")
    before = files(tmp_path)
    for operation in (questions.preflight_document, questions.publish_document):
        with pytest.raises(questions.QuestionRecordError, match="수동 상세"):
            operation(tmp_path, draft())
        assert files(tmp_path) == before


def test_edited_generated_detail_requires_manual_resolution_and_missing_detail_can_regenerate(tmp_path):
    saved = questions.publish_document(tmp_path, draft())
    detail = tmp_path / "details/ask-palette.md"
    detail.write_text(detail.read_text(encoding="utf-8") + "Manual note", encoding="utf-8")
    before = files(tmp_path)
    assert questions.inspect_document(tmp_path, "ask-palette.md", questions.render_document(saved))[1]
    with pytest.raises(questions.QuestionRecordError, match="수동 상세"):
        questions.render_saved_document(tmp_path, "ask-palette.md")
    assert files(tmp_path) == before
    detail.unlink()
    questions.render_saved_document(tmp_path, "ask-palette.md")
    assert detail.read_bytes() == questions.render_detail_document(saved).encode("utf-8")


def test_detail_failure_rolls_back_revision_snapshot_json_current_and_history(tmp_path, monkeypatch):
    saved = questions.publish_document(tmp_path, draft())
    revised = questions.patch_document(saved, {"set": {"revision": 2, "title": "New title"},
                                                "upsert_detail_blocks": [{"id": "evidence", "type": "paragraph", "text": "New detail"}]})
    before = files(tmp_path)
    original = questions.write_text

    def fail_detail(path, value):
        if path == tmp_path / "details/ask-palette.md":
            original(path, value)
            raise OSError("detail storage unavailable")
        original(path, value)

    monkeypatch.setattr(questions, "write_text", fail_detail)
    with pytest.raises(questions.QuestionRecordError, match="detail storage"):
        questions.publish_document(tmp_path, revised, 1, editorial=True)
    assert files(tmp_path) == before
    assert not (tmp_path / "questions/history/ask-palette/1.json").exists()


def test_removing_owned_details_is_transactional_and_leaves_manual_detail_alone(tmp_path):
    saved = questions.publish_document(tmp_path, draft())
    revised = questions.patch_document(saved, {"set": {"revision": 2}, "remove_detail_blocks": ["evidence"]})
    questions.publish_document(tmp_path, revised, 1, editorial=True)
    assert not (tmp_path / "details/ask-palette.md").exists()
    assert "Fictional display measurements" in (tmp_path / "details/history/ask-palette.md").read_text(encoding="utf-8")


def test_transaction_preserves_external_change_and_reports_rollback_gap(tmp_path, monkeypatch):
    saved = questions.publish_document(tmp_path, draft())
    revised = questions.patch_document(saved, {"set": {"revision": 2, "title": "New title"}})
    original = questions.write_text
    source = tmp_path / "questions/ask-palette.json"

    def external_edit(path, value):
        if path == tmp_path / "ask-palette.md":
            source.write_text("External change", encoding="utf-8")
            raise OSError("current document unavailable")
        original(path, value)

    monkeypatch.setattr(questions, "write_text", external_edit)
    with pytest.raises(questions.QuestionRecordError, match="롤백 확인 필요"):
        questions.publish_document(tmp_path, revised, 1, editorial=True)
    assert source.read_text(encoding="utf-8") == "External change"


@pytest.mark.parametrize("fail_detail", [False, True])
def test_restore_tracks_explicit_selection_and_generated_detail_in_same_rollback(tmp_path, monkeypatch, fail_detail):
    saved = questions.publish_document(tmp_path, draft())
    reviewed = copy.deepcopy(saved)
    reviewed["items"][0].update(status="answered", answer=reply()["updates"][0]["answer"], selected="blue")
    reviewed["answers"] = [reply()]
    reviewed["detail_blocks"][0]["text"] = "Recovered evidence."
    source = tmp_path / "questions/ask-palette.json"
    current = tmp_path / "ask-palette.md"
    current.write_text(current.read_text(encoding="utf-8") + "\n" + reply()["text"], encoding="utf-8")
    evidence = tmp_path / "evidence.md"
    evidence.write_bytes(current.read_bytes())
    recovery_input = tmp_path / "reviewed.json"
    recovery_input.write_text(json.dumps(reviewed), encoding="utf-8")
    before = files(tmp_path)
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if fail_detail:
        original = questions.write_bytes
        original_detail = (tmp_path / "details/ask-palette.md").read_bytes()

        def fail(path, value):
            if path == tmp_path / "details/ask-palette.md" and value != original_detail:
                raise OSError("detail write interrupted")
            original(path, value)

        monkeypatch.setattr(questions, "write_bytes", fail)
        with pytest.raises(questions.QuestionRecordError, match="detail write"):
            questions.restore_document(tmp_path, recovery_input, evidence, 1, expected_hash)
        assert all((tmp_path / name).read_bytes() == value for name, value in before.items())
    else:
        assert questions.restore_document(tmp_path, recovery_input, evidence, 1, expected_hash) == reviewed
        assert not questions.inspect_document(tmp_path, "ask-palette.md", questions.render_document(reviewed))[1]
    receipt, = (tmp_path / "questions/history/ask-palette/recovery").glob("*/receipt.json")
    saved_receipt = questions.read_json(receipt)
    assert saved_receipt["status"] == ("rolled_back" if fail_detail else "complete")
    assert saved_receipt["before_detail_existed"] and "after-detail.md" in saved_receipt["sha256"]
