"""slots / storage."""

import os

from chef.infrastructure.locking import FileLock
from chef.infrastructure.storage import atomic_write_text
from chef.slots.contracts import ROW_FIELDS, STATE_READY

ROWS_LOCK_TIMEOUT = 30


class Rows:
    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        header = lines[0].split("\t") if lines else []
        legacy = "state" not in header
        rows = []
        for line in lines[1:]:
            cells = line.split("\t")
            row = {key: "-" for key in ROW_FIELDS}
            row.update({column: cell for column, cell in zip(header, cells) if column in ROW_FIELDS})
            if legacy:
                row["state"] = STATE_READY
            if len(cells) >= 2 and row["name"] not in ("", "-"):
                rows.append(row)
        return rows

    def find(self, name):
        return next((row for row in self.read() if row["name"] == name), {})

    def upsert(self, name, **values):
        with FileLock(self.path + ".lock", "slots.tsv", ROWS_LOCK_TIMEOUT):
            rows = self.read()
            row = next((r for r in rows if r["name"] == name), None)
            if row is None:
                row = {key: "-" for key in ROW_FIELDS}
                row["name"] = name
                rows.append(row)
            row.update(values)
            self.write(rows)

    def remove(self, name):
        with FileLock(self.path + ".lock", "slots.tsv", ROWS_LOCK_TIMEOUT):
            self.write([row for row in self.read() if row["name"] != name])

    def write(self, rows):
        lines = ["\t".join(ROW_FIELDS)]
        lines.extend("\t".join(row.get(key, "-") or "-" for key in ROW_FIELDS) for row in rows)
        atomic_write_text(self.path, "\n".join(lines) + "\n", create_parents=True, creation_mode=0o666)
