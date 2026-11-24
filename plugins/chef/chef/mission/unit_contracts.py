"""Versioned unit brief identity without changing the units TSV format."""

import hashlib
import json
from chef.mission.contracts import EDITABLE_FIELDS, StateError, UNIT_HEADER, UNIT_ID, UNITS_FILE
from chef.mission.storage import read_table, require_unit, write_text
from chef.verification import references
from chef.verification.receipts import latest_receipt


def identity(row):
    brief = {name: row[name] for name in EDITABLE_FIELDS}
    encoded = json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def path_for(folder, unit):
    if not UNIT_ID.fullmatch(unit):
        raise StateError("contract unit id 형식 오류")
    return references.resolve_path(folder, f"contracts/{unit}.json")


def load(folder, row):
    path = path_for(folder, row["id"])
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        # A single atomic sidecar publication is a write-ahead journal. Readers
        # select the committed view using the atomically replaced units row, and
        # never repair files during read-only commands.
        if (not isinstance(value, dict) or type(value.get("schema")) is not int
                or value.get("schema") != 1 or value.get("unit") != row["id"]):
            raise ValueError("invalid contract sidecar")
        transition = value.get("transition")
        if transition is not None:
            if (not isinstance(transition, dict) or set(transition) != {"oldSha256", "previous"}
                    or not isinstance(value.get("current"), dict)):
                raise ValueError("invalid pending transition")
            row_hash = identity(row)
            if row_hash == transition["oldSha256"]:
                value = transition["previous"]
                if value is None:
                    return None
            elif row_hash == value["current"].get("sha256"):
                value = {key: item for key, item in value.items() if key != "transition"}
            else:
                raise ValueError("pending transition differs from both row versions")
        current = value["current"]
        if (type(value["schema"]) is not int or value["schema"] != 1 or value["unit"] != row["id"]
                or type(current["version"]) is not int or current["version"] < 2
                or current["sha256"] != identity(row) or not isinstance(value["history"], list)
                or not all(isinstance(event, dict) and type(event.get("reverificationNeeded")) is bool
                           for event in value["history"])):
            raise ValueError("current brief differs")
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise StateError(f"unit contract 읽기 실패: {path}: {error}") from error
    return value


def binding(folder, row):
    value = load(folder, row)
    return value["current"] if value else None


def prepare_change(folder, old, new, at, ledger):
    previous = load(folder, old)
    before = previous["current"] if previous else {"version": 1, "sha256": identity(old)}
    after = {"version": before["version"] + 1, "sha256": identity(new)}
    receipt_path, receipt = latest_receipt(folder, old["id"])
    passes = [{"at": item["at"], "grade": item["grade"], "evidence": item["evidence"]}
              for item in ledger if item["unit"] == old["id"] and item["status"] == "pass"]
    event = {"at": at, "old": before, "new": after,
             "reverificationNeeded": bool(passes or receipt.get("status") == "pass")}
    if passes:
        event["previousLedgerPass"] = passes[-1]
    if receipt.get("status") == "pass":
        event["previousReceipt"] = receipt_path
    value = {"schema": 1, "unit": old["id"], "current": after,
             "history": [*(previous["history"] if previous else []), event]}
    return value, event


def save(folder, row, value):
    path = path_for(folder, row["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def commit_change(folder, row, value, write_unit):
    path = path_for(folder, row["id"])
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    event = value["history"][-1]
    before = ({"schema": 1, "unit": value["unit"], "current": event["old"],
               "history": value["history"][:-1]} if event["old"]["version"] > 1 else None)
    journal = {**value, "transition": {"oldSha256": event["old"]["sha256"], "previous": before}}
    save(folder, row, journal)
    try:
        write_unit()
    except BaseException:
        # The writer may have replaced TSV before a later exception/signal.
        # Restore only when the old row is proven present; otherwise retain the
        # journal so either committed view remains recoverable.
        try:
            units = references.resolve_path(folder, UNITS_FILE)
            observed = require_unit(read_table(units, UNIT_HEADER), row["id"])
            if identity(observed) == event["old"]["sha256"]:
                if previous is None:
                    path.unlink()
                else:
                    write_text(path, previous)
        except (OSError, StateError):
            pass
        raise
    save(folder, row, value)


def status(folder, row):
    value = load(folder, row)
    if value is None:
        return None
    _, receipt = latest_receipt(folder, row["id"])
    passed_current = (receipt.get("status") == "pass"
                      and receipt.get("plan", {}).get("unitContract") == value["current"]
                      and receipt.get("plan", {}).get("unitBrief") == {key: row[key] for key in EDITABLE_FIELDS})
    return {"current": value["current"], "reverificationNeeded":
            not passed_current and any(event.get("reverificationNeeded") for event in value["history"])}
