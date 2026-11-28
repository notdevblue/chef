"""Checks for draft and shared mission documents."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from chef.infrastructure.paths import inside_real_root
from chef.mission import schema_tables
from chef.mission.contracts import StateError
from chef.questions.authored import AuthoringInput
from chef.questions.blocks import QuestionRecordError

KIND_MARKER = "<!-- chef:document "
SECTIONS = {
    "report": ("지금", "남은 확인", "다음 일"),
    "detail": ("범위", "근거"),
}
HEADING = re.compile(r"^## ([^\n]+?)[ \t]*$", re.MULTILINE)
LINK = re.compile(r"\]\((?:<([^>\n]+)>|([^\s()]+))(?:[ \t]+\"[^\"\n]*\")?\)")
SCHEMA_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,70}")


def draft(kind, title):
    if not title.strip() or any(char in title for char in "\r\n\x00"):
        raise StateError("document title is required and must be one line")
    sections = "".join(f"\n## {section}\n\n" for section in SECTIONS[kind])
    return f"# {title.strip()}\n\n{KIND_MARKER}{kind} -->\n{sections}"


def schema_text(block):
    issues = AuthoringInput()
    issues.block(block, "schema-table")
    if issues.issues:
        raise StateError("; ".join(issues.issues))
    if block["type"] != schema_tables.SCHEMA_TABLE:
        raise StateError("schema documents require a single schema-table block")
    # Escape HTML comment delimiters in metadata without changing the JSON values.
    payload = json.dumps(block, ensure_ascii=False, sort_keys=True).replace("-", "\\u002d")
    return f"# {block['title']}\n\n{schema_tables.TABLE_MARKER}{payload} -->\n\n{schema_tables.render(block)}\n"


def check(name, path, content, folder, project, adapter):
    result = {"path": name, "sha256": hashlib.sha256(content).hexdigest(),
              "issues": [], "schemaChecks": []}
    if not name.casefold().endswith(".md"):
        result["checked"] = "attachment"
        return result
    result["checked"] = "markdown"
    try:
        text = content.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeError:
        result["issues"].append("document is not UTF-8")
        return result
    errors = result["issues"]
    markers = [line for line in text.splitlines() if line.startswith(schema_tables.TABLE_MARKER)]
    if markers:
        try:
            if len(markers) != 1 or not markers[0].endswith(" -->"):
                raise StateError("schema document needs exactly one table block")
            block = json.loads(markers[0][len(schema_tables.TABLE_MARKER):-4])
            if schema_text(block) != text:
                raise StateError("schema document differs from its generated table")
            result["schemaChecks"] = [schema_tables.check_block(block, project, adapter)]
        except (StateError, QuestionRecordError, ValueError, OSError) as error:
            errors.append(str(error))
        return result
    if not re.search(r"^# [^\s].*", text, re.MULTILINE):
        errors.append("document needs a level-one title")
    kinds = re.findall(r"^<!-- chef:document (report|detail) -->$", text, re.MULTILINE)
    if KIND_MARKER in text and (len(kinds) != 1 or text.count(KIND_MARKER) != 1):
        errors.append("document needs one valid kind marker")
    if len(kinds) == 1:
        sections = list(HEADING.finditer(text))
        for expected in SECTIONS[kinds[0]]:
            matches = [index for index, section in enumerate(sections) if section[1] == expected]
            if len(matches) != 1:
                errors.append(f"expected one section: {expected}")
                continue
            index = matches[0]
            end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
            body = re.sub(r"<!--.*?-->", "", text[sections[index].end():end], flags=re.DOTALL)
            if not body.strip():
                errors.append(f"empty section: {expected}")
    prose = re.sub(r"(?ms)^ *(```+|~~~+).*?^ *\1[ \t]*$", "", text)
    if re.search(r"<\s*table\b", prose, re.IGNORECASE):
        errors.append("use Markdown for comparison tables or generated documents for schema definitions")
    lines = prose.splitlines()
    for index, line in enumerate(lines[:-1]):
        if "|" in line and re.fullmatch(r"[ |:\-]+", lines[index + 1]) and "-" in lines[index + 1]:
            if schema_tables.schema_columns(line.strip().strip("|").split("|")):
                errors.append(f"line {index + 1}: use a generated schema-table document for table definitions")
    for match in LINK.finditer(prose):
        raw = match[1] or match[2]
        try:
            parsed = urlsplit(raw)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                continue
            link = unquote(parsed.path)
            if not link and not parsed.netloc and not parsed.scheme:
                continue
            if (parsed.scheme or parsed.netloc or Path(link).is_absolute() or "\\" in link
                    or not inside_real_root(folder, Path(path).parent / link)):
                errors.append(f"link leaves the mission folder: {raw}")
            elif not (Path(path).parent / link).is_file():
                errors.append(f"missing link target: {raw}")
        except (OSError, ValueError) as error:
            errors.append(f"invalid link {raw}: {error}")
    return result
