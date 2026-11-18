"""Validate and render optional, domain-neutral question document blocks."""

import re


class QuestionRecordError(ValueError):
    pass


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(required) - set(optional):
        raise QuestionRecordError(f"structured fields invalid; required={required}, optional={optional}")


def text(value, label, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise QuestionRecordError(f"{label} must be a string")
    return value


def identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise QuestionRecordError(f"{label} id invalid: {value}")
    return value


def validate_options(item):
    if "options" in item:
        options = item["options"]
        if not isinstance(options, list) or not options:
            raise QuestionRecordError("options must be a nonempty array")
        seen = set()
        for option in options:
            fields(option, ("id", "label"), ("description", "impact"))
            identifier(option["id"], "option")
            if option["id"] in seen:
                raise QuestionRecordError(f"duplicate option id: {option['id']}")
            seen.add(option["id"])
            text(option["label"], "option label")
            for name in ("description", "impact"):
                if name in option:
                    text(option[name], f"option {name}", False)
    if "selected" in item:
        identifier(item["selected"], "selected")
        if item["status"] != "answered":
            raise QuestionRecordError("selected requires answered status")
        if item["selected"] not in {option["id"] for option in item.get("options", [])}:
            raise QuestionRecordError("selected must name an existing option id")


def validate_blocks(record):
    seen = set()
    references = []
    field_ids = set()
    for name in ("blocks", "detail_blocks"):
        if name not in record:
            continue
        blocks = record[name]
        if not isinstance(blocks, list):
            raise QuestionRecordError(f"{name} must be an array")
        for block in blocks:
            if not isinstance(block, dict):
                raise QuestionRecordError("block must be an object")
            kind = block.get("type")
            content = {"paragraph": "text", "table": "columns", "fields": "fields"}.get(kind) if isinstance(kind, str) else None
            if content is None:
                raise QuestionRecordError(f"unknown block type: {kind}")
            required = ("id", "type", content)
            if kind == "table":
                required += ("rows",)
            fields(block, required, ("title",))
            identifier(block["id"], "block")
            if block["id"] in seen:
                raise QuestionRecordError(f"duplicate block id: {block['id']}")
            seen.add(block["id"])
            if "title" in block:
                title = text(block["title"], "block title")
                if title.splitlines() != [title]:
                    raise QuestionRecordError("block title must be one line")
            if kind == "paragraph":
                text(block["text"], "paragraph text")
            elif kind == "table":
                columns = block["columns"]
                if not isinstance(columns, list) or not columns:
                    raise QuestionRecordError("table columns must be a nonempty array")
                for column in columns:
                    text(column, "table column")
                if len(columns) != len(set(columns)):
                    raise QuestionRecordError("duplicate table column")
                if not isinstance(block["rows"], list):
                    raise QuestionRecordError("table rows must be an array")
                for row in block["rows"]:
                    if not isinstance(row, list) or len(row) != len(columns):
                        raise QuestionRecordError("table row width differs from columns")
                    for cell in row:
                        text(cell, "table cell", False)
            else:
                definitions = block["fields"]
                if not isinstance(definitions, list) or not definitions:
                    raise QuestionRecordError("fields must be a nonempty array")
                for definition in definitions:
                    fields(definition, ("name", "type"), ("key", "ref", "description"))
                    identifier(definition["name"], "field name")
                    target = f"{block['id']}.{definition['name']}"
                    if target in field_ids:
                        raise QuestionRecordError(f"duplicate field name: {target}")
                    field_ids.add(target)
                    text(definition["type"], "field type")
                    for key in ("key", "description"):
                        if key in definition:
                            text(definition[key], f"field {key}", False)
                    if "ref" in definition:
                        references.append(text(definition["ref"], "field ref"))
    for reference in references:
        if reference not in field_ids:
            raise QuestionRecordError(f"unknown field ref: {reference}")
    if "blocks" in record and record["body"] != "":
        raise QuestionRecordError("body must be empty when blocks is present")


def cell(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def table(columns, rows):
    lines = ["| " + " | ".join(cell(value) for value in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_blocks(blocks):
    sections = []
    for block in blocks:
        sections.append(f"<a id=\"{block['id']}\"></a>")
        if "title" in block:
            sections.append("## " + block["title"])
        if block["type"] == "paragraph":
            sections.append(block["text"])
        elif block["type"] == "table":
            sections.append(table(block["columns"], block["rows"]))
        else:
            columns = ("name", "type", "key", "ref", "description")
            sections.append(table(columns, [[definition.get(name, "") for name in columns] for definition in block["fields"]]))
    return "\n\n".join(sections)


def render_options(options):
    return table(("id", "선택", "설명", "영향"), [[option["id"], option["label"], option.get("description", ""), option.get("impact", "")] for option in options])


def render_selection(item):
    option = next(option for option in item["options"] if option["id"] == item["selected"])
    return table(("문항", "선택 id", "현재 선택", "설명", "영향"), [[item["title"], option["id"], option["label"], option.get("description", ""), option.get("impact", "")]])
