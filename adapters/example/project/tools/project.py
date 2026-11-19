"""Small setup and verification commands for the fictional Comet project."""

import argparse
import runpy
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "verify"))
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    source = root / "src/arithmetic.py"
    if not source.is_file():
        raise SystemExit("missing source input")
    if args.command == "setup":
        generated = root / "generated"
        generated.mkdir(exist_ok=True)
        marker = generated / "ready.txt"
        if marker.exists() and marker.read_text(encoding="utf-8") != "Comet ready\n":
            raise SystemExit("existing setup marker conflicts")
        if not marker.exists():
            marker.write_text("Comet ready\n", encoding="utf-8")
    else:
        compile(source.read_text(encoding="utf-8"), str(source), "exec")
        assert runpy.run_path(str(source))["double"](7) == 14
    print(args.command + " passed")


if __name__ == "__main__":
    main()
