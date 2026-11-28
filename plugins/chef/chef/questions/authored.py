"""Schema 2 authoring constraints and rendering, independent of saved answer text."""

import re
from html import escape
from urllib.parse import urlsplit

from chef.questions.blocks import QuestionRecordError, table
from chef.mission import schema_tables


TITLE_LIMIT = 80
LABEL_LIMIT = 40
PROSE_LIMIT = 160
ITEM_LIMIT = 320
BLOCK_TYPES = {"paragraph", "list", "rules", "steps", "table", "fields", "literal", schema_tables.SCHEMA_TABLE}
ANCHOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
BLOCK_MARKUP = re.compile(
    r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```|~~~|\|)"
    r"|</?(?:div|span|a|script|table|p|br|pre|code|img|iframe)\b[^>]*>"
    r"|!?(?:\[[^]]*\]\([^)]*\))", re.IGNORECASE
)
ENTRY_OPTIONAL = {"list": (), "rules": ("when", "conditions", "exception_of"), "steps": ("failure",)}
BLOCK_OPTIONAL = {"rules": ("priority",), "table": ("row_ids",)}


class AuthoringInput:
    """Collect independent input errors so a batch can be repaired in one edit."""

    def __init__(self):
        self.issues = []
        self.targets = set()
        self.rule_targets = set()
        self.references = []
        self.exception_edges = {}

    def problem(self, path, message):
        self.issues.append(f"{path}: {message}")

    def shape(self, value, path, required, optional=()):
        if not isinstance(value, dict):
            self.problem(path, "객체가 필요합니다")
            return False
        for key in sorted(set(required) - value.keys()):
            self.problem(f"{path}.{key}", "필수 필드입니다")
        for key in sorted(value.keys() - set(required) - set(optional)):
            self.problem(f"{path}.{key}", "지원하지 않는 필드입니다")
        return True

    def short(self, value, path, limit=None, required=True):
        if not isinstance(value, str):
            self.problem(path, "문자열이 필요합니다")
            return
        if required and not value.strip():
            self.problem(path, "빈 문자열을 사용할 수 없습니다")
        if limit is not None and len(value) > limit:
            self.problem(path, f"{len(value)}자 / 최대 {limit}자")
        if any(ord(char) < 32 for char in value) or BLOCK_MARKUP.search(value):
            self.problem(path, "한 줄의 산문만 사용하세요. 목록·표·코드·링크는 정형 블록으로 작성하세요")

    def array(self, value, path, required=True):
        if not isinstance(value, list) or (required and not value):
            self.problem(path, "비어 있지 않은 배열이 필요합니다" if required else "배열이 필요합니다")
            return []
        return value

    def target(self, value, path, prefix=""):
        if not isinstance(value, str) or not ANCHOR.fullmatch(value):
            self.problem(path, "영문·숫자·하이픈·밑줄로 1..80자 ID를 지정하세요")
            return ""
        target = prefix + value
        if target in self.targets:
            self.problem(path, f"중복 ID {target}")
        self.targets.add(target)
        return target

    def reference(self, value, path):
        if not isinstance(value, str):
            self.problem(path, "참조 ID 문자열이 필요합니다")
        else:
            self.references.append((value, path))

    def entries(self, block, path):
        kind = block["type"]
        prefix = f"{block.get('id', '')}."
        ordered = []
        for index, entry in enumerate(self.array(block.get("items"), f"{path}.items")):
            ep = f"{path}.items[{index}]"
            content = "result" if kind == "rules" else "text"
            optional = ENTRY_OPTIONAL[kind]
            if not self.shape(entry, ep, ("id", "label", content), (*optional, "refs")):
                continue
            target = self.target(entry.get("id"), f"{ep}.id", prefix)
            if kind == "rules":
                self.rule_targets.add(target)
            ordered.append(target)
            self.short(entry.get("label"), f"{ep}.label", LABEL_LIMIT)
            self.short(entry.get(content), f"{ep}.{content}")
            for key in ("when", "failure"):
                if key in entry:
                    self.short(entry[key], f"{ep}.{key}")
            if "conditions" in entry:
                conditions = entry["conditions"]
                if self.shape(conditions, f"{ep}.conditions", ("mode", "items")):
                    if conditions.get("mode") not in ("all", "any"):
                        self.problem(f"{ep}.conditions.mode", "all 또는 any를 지정하세요")
                    for ci, condition in enumerate(self.array(conditions.get("items"), f"{ep}.conditions.items")):
                        self.short(condition, f"{ep}.conditions.items[{ci}]")
                if "when" in entry:
                    self.problem(ep, "when과 conditions 중 하나를 사용하세요")
            if "exception_of" in entry:
                self.reference(entry["exception_of"], f"{ep}.exception_of")
                if isinstance(entry["exception_of"], str):
                    self.exception_edges[target] = entry["exception_of"]
                if not entry.get("when") and not entry.get("conditions"):
                    self.problem(ep, "예외 항목에는 적용 조건이 필요합니다")
            for ri, ref in enumerate(self.array(entry.get("refs", []), f"{ep}.refs", False)):
                self.reference(ref, f"{ep}.refs[{ri}]")
        if "priority" in block:
            priority = self.array(block["priority"], f"{path}.priority")
            expected = [value.removeprefix(prefix) for value in ordered]
            if not all(isinstance(value, str) for value in priority) or sorted(priority) != sorted(expected):
                self.problem(f"{path}.priority", "각 규칙 ID를 우선순위 순서로 정확히 한 번씩 지정하세요")

    def block(self, block, path):
        if not isinstance(block, dict):
            self.problem(path, "블록 객체가 필요합니다")
            return
        kind = block.get("type")
        if not isinstance(kind, str) or kind not in BLOCK_TYPES:
            self.problem(f"{path}.type", "paragraph/list/rules/steps/table/fields/schema-table/literal 중 하나를 사용하세요")
            return
        if kind == "paragraph":
            if self.shape(block, path, ("id", "type", "sentences"), ("title",)):
                self.target(block.get("id"), f"{path}.id")
                if "title" in block:
                    self.short(block["title"], f"{path}.title", TITLE_LIMIT)
                for index, sentence in enumerate(self.array(block.get("sentences"), f"{path}.sentences")):
                    self.short(sentence, f"{path}.sentences[{index}]")
                    if isinstance(sentence, str) and (
                        re.fullmatch(r"\s*(?:-\s*){3,}|\s*(?:\*\s*){3,}|\s*(?:_\s*){3,}", sentence)
                        or sentence.startswith('    ')
                    ):
                        self.problem(f"{path}.sentences[{index}]", "문단에 블록 Markdown을 사용할 수 없습니다")
                sentences = block.get('sentences')
                if isinstance(sentences, list) and all(isinstance(value, str) for value in sentences):
                    paragraph = ' '.join(sentences)
                    if BLOCK_MARKUP.search(paragraph) or re.fullmatch(r"\s*(?:-\s*){3,}|\s*(?:\*\s*){3,}|\s*(?:_\s*){3,}", paragraph):
                        self.problem(f"{path}.sentences", "문단에 블록 Markdown을 사용할 수 없습니다")
            return
        content = {"table": ("columns", "rows"), "fields": ("fields",), "literal": ("format", "value"),
                   schema_tables.SCHEMA_TABLE: ("target", "fields")}.get(kind, ("items",))
        optional = BLOCK_OPTIONAL.get(kind, ())
        if not self.shape(block, path, ("id", "type", "title", *content), optional):
            return
        self.target(block.get("id"), f"{path}.id")
        self.short(block.get("title"), f"{path}.title", TITLE_LIMIT)
        if kind in ("list", "rules", "steps"):
            self.entries(block, path)
        elif kind == "table":
            columns = self.array(block.get("columns"), f"{path}.columns")
            for index, column in enumerate(columns):
                self.short(column, f"{path}.columns[{index}]", LABEL_LIMIT)
            if all(isinstance(column, str) for column in columns) and len(set(columns)) != len(columns):
                self.problem(f"{path}.columns", "열 이름이 중복됩니다")
            for index, row in enumerate(self.array(block.get("rows"), f"{path}.rows", False)):
                if not isinstance(row, list) or len(row) != len(columns):
                    self.problem(f"{path}.rows[{index}]", "열 수와 셀 수가 다릅니다")
                    continue
                for ci, value in enumerate(row):
                    self.short(value, f"{path}.rows[{index}][{ci}]", required=False)
            if "row_ids" in block:
                ids = self.array(block["row_ids"], f"{path}.row_ids", False)
                if not isinstance(block.get("rows"), list) or len(ids) != len(block["rows"]):
                    self.problem(f"{path}.row_ids", "행마다 ID를 하나씩 지정하세요")
                for index, value in enumerate(ids):
                    self.target(value, f"{path}.row_ids[{index}]", f"{block.get('id', '')}.")
        elif kind in ("fields", schema_tables.SCHEMA_TABLE):
            if kind == schema_tables.SCHEMA_TABLE:
                try:
                    schema_tables.validate_target(block["target"])
                except QuestionRecordError as error:
                    self.problem(f"{path}.target", str(error))
            for index, field in enumerate(self.array(block.get("fields"), f"{path}.fields")):
                fp = f"{path}.fields[{index}]"
                optional = ("key", "ref", "description", "constraint") if kind == schema_tables.SCHEMA_TABLE else (
                    "key", "ref", "description"
                )
                if self.shape(field, fp, ("name", "type"), optional):
                    self.target(field.get("name"), f"{fp}.name", f"{block.get('id', '')}.")
                    for key in ("type", "key", "description", "constraint"):
                        if key in field or key == "type":
                            self.short(field.get(key), f"{fp}.{key}", required=key == "type")
                    if "ref" in field:
                        self.reference(field["ref"], f"{fp}.ref")
        else:
            self.literal(block, path)

    def literal(self, block, path):
        kind, value = block.get("format"), block.get("value")
        if kind not in ("identifier", "formula", "code", "link"):
            self.problem(f"{path}.format", "identifier/formula/code/link 중 하나를 사용하세요")
        limit = 8192 if kind == "code" else 2048
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            self.problem(f"{path}.value", f"비어 있지 않은 문자열, 최대 {limit}자가 필요합니다")
            return
        if kind == "identifier" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_./:+<>\[\],-]*", value):
            self.problem(f"{path}.value", "공백 없는 기술 식별자가 필요합니다")
        if kind == "link":
            try:
                url = urlsplit(value)
                web_url = url.scheme in ("http", "https") and bool(url.netloc)
                relative = not url.scheme and not url.netloc and not value.startswith(("/", "\\"))
                valid = web_url or relative
            except ValueError:
                valid = False
            if not valid or any(char.isspace() or ord(char) < 32 for char in value):
                self.problem(f"{path}.value", "http(s) URL 또는 상대 링크를 사용하세요")

    def finish(self):
        for ref, path in self.references:
            if ref not in self.targets:
                self.problem(path, f"존재하지 않는 참조 {ref}")
        for start in self.exception_edges:
            if self.exception_edges[start] not in self.rule_targets:
                self.problem(start, "예외의 대상 규칙이 필요합니다")
            visited = set()
            target = start
            while target in self.exception_edges and target not in visited:
                visited.add(target)
                target = self.exception_edges[target]
            if target in visited:
                self.problem(start, "예외 참조가 순환합니다")
        if self.issues:
            raise QuestionRecordError("작성 입력 오류\n" + "\n".join(self.issues))


def approval_from_answer(record):
    answers = record.get("answers", [])
    if not isinstance(answers, list):
        return False
    for answer in reversed(answers):
        if isinstance(answer, dict) and "approval" in answer:
            return answer["approval"] == record.get("approval")
    return False


def validate_authored(record):
    check = AuthoringInput()
    check.short(record.get("title"), "title", TITLE_LIMIT)
    check.short(record.get("id"), "id", TITLE_LIMIT)
    check.short(record.get("intro"), "intro", required=False)
    if record.get("body") != "":
        check.problem("body", "빈 문자열을 두고 정형 blocks를 작성하세요")
    approval = record.get("approval")
    if isinstance(approval, dict) and not approval_from_answer(record):
        check.short(approval.get("reason"), "approval.reason", required=False)
    for i, item in enumerate(check.array(record.get("items"), "items")):
        path = f"items[{i}]"
        if not isinstance(item, dict):
            check.problem(path, "문항 객체가 필요합니다")
            continue
        check.short(item.get("title"), f"{path}.title", TITLE_LIMIT)
        check.short(item.get("id"), f"{path}.id", TITLE_LIMIT)
        check.short(item.get("body"), f"{path}.body", required=False)
        if "heading" in item:
            check.short(item["heading"], f"{path}.heading", TITLE_LIMIT)
        for oi, option in enumerate(check.array(item.get("options", []), f"{path}.options", False)):
            op = f"{path}.options[{oi}]"
            if not isinstance(option, dict):
                check.problem(op, "선택지 객체가 필요합니다")
                continue
            check.short(option.get("label"), f"{op}.label", LABEL_LIMIT)
            check.short(option.get("id"), f"{op}.id", TITLE_LIMIT)
            for key in ("description", "impact"):
                if key in option:
                    check.short(option[key], f"{op}.{key}", required=False)
    for name in ("blocks", "detail_blocks"):
        earlier_targets = set(check.targets)
        start = len(check.references)
        for index, block in enumerate(check.array(record.get(name, []), name, name == "blocks")):
            check.block(block, f"{name}[{index}]")
        local_targets = check.targets - earlier_targets
        for ref, path in check.references[start:]:
            if ref not in local_targets:
                check.problem(path, "참조는 같은 출력 문서 안의 항목을 가리켜야 합니다")
    check.finish()


def anchor(target):
    return '<a id="' + escape(target, quote=True) + '"></a>'


def render_authored_block(block):
    kind = block["type"]
    parts = [anchor(block["id"])]
    if "title" in block:
        parts.append("## " + escape(block["title"]))
    if kind == "paragraph":
        parts.append(" ".join(escape(sentence) for sentence in block["sentences"]))
    elif kind == "table":
        rows = block["rows"]
        if "row_ids" in block:
            # Anchors are renderer-owned markup, added after escaping the user cells.
            rows = [list(row) for row in rows]
            rendered = table(block["columns"], rows).splitlines()
            for index, row_id in enumerate(block["row_ids"]):
                target = f"{block['id']}.{row_id}"
                rendered[index + 2] = rendered[index + 2].replace("| ", "| " + anchor(target), 1)
            parts.append("\n".join(rendered))
        else:
            parts.append(table(block["columns"], rows))
    elif kind == schema_tables.SCHEMA_TABLE:
        parts.append(schema_tables.render(block))
        parts.extend(anchor(f"{block['id']}.{field['name']}") for field in block["fields"])
    elif kind == "fields":
        columns = ("name", "type", "key", "ref", "description")
        parts.append(table(columns, [[field.get(key, "") for key in columns] for field in block["fields"]]))
        parts.extend(anchor(f"{block['id']}.{field['name']}") for field in block["fields"])
    elif kind == "literal":
        if block["format"] == "link":
            parts.append(f'<a href="{escape(block["value"], quote=True)}">{escape(block["title"])}</a>')
        else:
            parts.append('<pre style="white-space:pre-wrap;overflow-wrap:anywhere"><code>' + escape(block["value"]) + '</code></pre>')
    else:
        entries = block["items"]
        if "priority" in block:
            indexed = {entry["id"]: entry for entry in entries}
            entries = [indexed[key] for key in block["priority"]]
            parts.append("위에서부터 우선 적용합니다.")
        for index, entry in enumerate(entries, 1):
            marker = f"{index}." if kind == "steps" else "-"
            target = f"{block['id']}.{entry['id']}"
            lines = [f"{marker} {anchor(target)} **{escape(entry['label'])}**"]
            if entry.get("when"):
                lines.append("  적용 조건 · " + escape(entry["when"]))
            if "conditions" in entry:
                mode = "모두 충족" if entry["conditions"]["mode"] == "all" else "하나 이상 충족"
                lines.append("  적용 조건 · " + mode)
                for number, value in enumerate(entry["conditions"]["items"], 1):
                    lines.append(f"  조건 {number} · {escape(value)}")
            lines.append("  " + escape(entry["result" if kind == "rules" else "text"]))
            if entry.get("failure"):
                lines.append("  실패 시 · " + escape(entry["failure"]))
            if "exception_of" in entry:
                ref = entry["exception_of"]
                lines.append(f"  예외 대상 · [{escape(ref)}](#{ref})")
            references = entry.get("refs", [])
            if references:
                lines.append("  관련 항목 · " + " · ".join(f'[{escape(ref)}](#{ref})' for ref in references))
            parts.append("  \n".join(lines))
    return "\n\n".join(parts)


def render_choice(option, compact=False):
    if compact:
        parts = [escape(option[key]) for key in ("description", "impact") if option.get(key)]
        return "**" + escape(option["label"]) + "**" + (" — " + " ".join(parts) if parts else "")
    lines = ["- **" + escape(option["label"]) + "**"]
    for key, label in (("description", "설명"), ("impact", "영향")):
        if option.get(key):
            lines.append(f"  {label} · {escape(option[key])}")
    return "  \n".join(lines)
