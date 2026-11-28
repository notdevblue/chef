"""Table ownership, rendering and optional project catalogue checks."""

import hashlib
import json
import re
from pathlib import Path

from chef.infrastructure.paths import inside_real_root
from chef.questions.blocks import QuestionRecordError, table

SCHEMA_TABLE = "schema-table"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,119}")
COLUMNS = ("name", "type", "key", "ref", "constraint", "description")
TABLE_MARKER = "<!-- chef:schema-table "
CATALOG_LIMIT = 4 * 1024 * 1024


def validate_target(target):
    if (not isinstance(target, dict) or set(target) - {"kind", "name", "state", "source", "sha256"}
            or target.get("kind") not in ("db", "proto")
            or not isinstance(target.get("name"), str) or not IDENTIFIER.fullmatch(target["name"])
            or target.get("state") not in ("existing", "proposed")):
        raise QuestionRecordError("target needs one kind (db/proto), table name and state (existing/proposed)")
    if "source" in target and (target["state"] != "proposed" or not isinstance(target["source"], str)
                               or not target["source"].strip()):
        raise QuestionRecordError("source may only name a proposed schema catalogue")
    if "sha256" in target and (not isinstance(target["sha256"], str)
                               or not re.fullmatch(r"[0-9a-f]{64}", target["sha256"])):
        raise QuestionRecordError("target sha256 must be a SHA-256 digest")


def render(block):
    target = block["target"]
    name = target["name"]
    rows = [[field.get(key, "") for key in COLUMNS] for field in block["fields"]]
    return "\n\n".join((f"`{target['kind'].upper()} {name}` · {target['state']}", table(COLUMNS, rows)))


def schema_columns(columns):
    names = {str(column).strip(" `*_").casefold() for column in columns}
    return (
        bool(names & {"type", "타입", "자료형", "데이터 타입"})
        and bool(names & {"column", "column name", "field", "field name", "name", "컬럼", "칼럼", "열", "필드"})
    )


def catalogue(project, source, pinned):
    if not isinstance(source, str) or not source.strip():
        raise QuestionRecordError("schema catalogue path must be a nonempty project-relative path")
    path = Path(source)
    if path.is_absolute() or path.drive:
        raise QuestionRecordError("schema catalogue must use a project-relative path")
    path = (Path(project) / path).resolve()
    if not inside_real_root(project, path):
        raise QuestionRecordError("schema catalogue escaped the project")
    try:
        with path.open("rb") as stream:
            data = stream.read(CATALOG_LIMIT + 1)
    except FileNotFoundError:
        return None
    if len(data) > CATALOG_LIMIT:
        raise QuestionRecordError("schema catalogue exceeds its size limit")
    digest = hashlib.sha256(data).hexdigest()
    if pinned and pinned != digest:
        raise QuestionRecordError("schema catalogue changed since the recorded SHA-256")
    try:
        value = json.loads(data)
    except ValueError as error:
        raise QuestionRecordError(f"invalid schema catalogue: {path}") from error
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("tables"), list):
        raise QuestionRecordError("schema catalogue needs schema 1 and a tables array")
    return value["tables"], digest


def check_block(block, project, adapter):
    target = block["target"]
    policies = adapter.get("documents", {})
    if not isinstance(policies, dict):
        raise QuestionRecordError("adapter documents must be an object")
    source = policies.get("schemaCatalog") if target["state"] == "existing" else target.get("source")
    result = {"id": block["id"], "kind": target["kind"], "table": target["name"]}
    if source is None:
        return {**result, "status": "unverified", "reason": "catalogue is not configured"}
    checked = catalogue(project, source, target.get("sha256"))
    if checked is None:
        return {**result, "status": "unverified", "reason": "catalogue file was not found"}
    tables, digest = checked
    matches = [item for item in tables if isinstance(item, dict)
               and (item.get("kind"), item.get("name")) == (target["kind"], target["name"])]
    if len(matches) != 1:
        raise QuestionRecordError(f"{block['id']}: expected exactly one table in the catalogue: {target['name']}")
    fields = matches[0].get("fields")
    if not isinstance(fields, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
        raise QuestionRecordError(f"{block['id']}: catalogue fields must map field names to types")
    for field in block["fields"]:
        if fields.get(field["name"]) != field["type"]:
            raise QuestionRecordError(
                f"{block['id']}: {field['name']} does not match a field of {target['kind']} {target['name']}"
            )
    return {**result, "status": "verified", "sourceSha256": digest}


def check_blocks(record, project, adapter, previous=None, *, require_typed=False):
    checks = []
    for area in ("blocks", "detail_blocks"):
        previous_blocks = (previous or {}).get(area, [])
        for block in record.get(area, []):
            if block["type"] == SCHEMA_TABLE:
                checks.append(check_block(block, project, adapter))
            elif require_typed and block["type"] in ("fields", "table") and block not in previous_blocks:
                raise QuestionRecordError(f"{area}/{block['id']}: use a schema-table block for table definitions")
    return checks
