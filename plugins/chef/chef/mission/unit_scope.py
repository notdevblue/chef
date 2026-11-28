"""Read file paths from a unit brief without treating prose as write scope."""

import json
from pathlib import Path, PureWindowsPath

from chef.infrastructure.paths import inside_real_root


def files(project, cell):
    structured = cell.startswith("[")
    try:
        entries = json.loads(cell) if structured else [cell]
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    root = Path(project).resolve()
    result = []
    for entry in entries:
        if not isinstance(entry, str) or not entry or entry != entry.strip():
            return []
        value = entry.replace("\\", "/")
        if (PureWindowsPath(value).anchor or any(char in value for char in '*?[]:"<>|')
                or any(ord(char) < 32 for char in value)
                or any(part in ("", ".", "..") for part in value.split("/"))):
            return []
        target = root / value
        if (not inside_real_root(root, target) or target.resolve() != target
                or target.is_dir() or (not structured and not target.is_file())):
            return []
        result.append(value)
    return list(dict.fromkeys(result))
