"""Generate the existing CLI paths from one runtime/module inventory."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/chef"
INVENTORY = Path(__file__).with_name("entrypoints.json")


def render(relative, entry):
    depth = len(Path(relative).parts) - 1
    bytecode = "sys.dont_write_bytecode = True\n" if entry.get("noBytecode") else ""
    return (
        f'"""Compatibility entry point; implementation: {entry["module"]}."""\n'
        "import sys\nfrom pathlib import Path\n\n"
        + bytecode
        + f"sys.path.insert(0, str(Path(__file__).resolve().parents[{depth}]))\n"
        "from chef.cli.entrypoints import expose\n\n"
        f"expose(globals(), {entry['module']!r}, runtime={entry.get('runtime')!r})\n"
    )


def check(write=False):
    entries = json.loads(INVENTORY.read_text(encoding="utf-8"))
    errors = []
    for relative, entry in entries.items():
        path = PLUGIN / relative
        target = PLUGIN / (entry["module"].replace(".", "/") + ".py")
        if not target.is_file():
            errors.append(f"missing implementation: {relative}: {entry['module']}")
            continue
        expected = render(relative, entry)
        if write:
            path.write_text(expected, encoding="utf-8", newline="\n")
        elif not path.is_file() or path.read_text(encoding="utf-8") != expected:
            errors.append(f"entry point differs from inventory: {relative}")
    return len(entries), errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite compatibility entry points")
    args = parser.parse_args()
    count, errors = check(args.write)
    for error in errors:
        print(error)
    if errors:
        return 1
    print(f"{count} runtime entry points verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
