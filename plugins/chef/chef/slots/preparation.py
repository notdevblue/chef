"""Portable, non-destructive preparation operations for personal adapters."""

import argparse
import json
import os
import platform
import shutil
from pathlib import Path

from chef.infrastructure.paths import is_link, same_path, inside_real_root


class PreparationError(ValueError):
    pass


def preflight(executables=(), files=(), *, cwd=None):
    """Validate external setup/build inputs before running any preparation."""
    for value in executables:
        path_value = str(value)
        if not os.path.isabs(path_value) and any(separator in path_value for separator in (os.sep, os.altsep) if separator):
            path_value = str(Path(cwd or os.getcwd()) / path_value)
        resolved = shutil.which(path_value)
        if not resolved or not Path(resolved).is_file():
            raise PreparationError("required executable is unavailable: " + value)
    for value in files:
        path = Path(value)
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        if is_link(path) or not path.is_file():
            raise PreparationError("required regular script/input is unavailable: " + value)
    return {"status": "ready"}


def cache_compatible(source_fingerprint, expected_fingerprint):
    """Caches are optional: unknown or incompatible inputs always trigger reimport."""
    try:
        if any(is_link(path) or not Path(path).is_file() for path in (source_fingerprint, expected_fingerprint)):
            return False
        source = json.loads(Path(source_fingerprint).read_text(encoding="utf-8"))
        expected = json.loads(Path(expected_fingerprint).read_text(encoding="utf-8"))
        required = ("os", "architecture", "toolVersion", "backend", "inputs")
        if not isinstance(source, dict) or not isinstance(expected, dict):
            return False
        if any(not isinstance(expected.get(key), str) or not expected[key] for key in required):
            return False
        return (source == expected and expected["os"] == platform.system().lower()
                and expected["architecture"] == platform.machine().lower())
    except (OSError, ValueError, TypeError):
        return False


def checked_tree(source):
    source = Path(source)
    if is_link(source) or not source.is_dir():
        raise PreparationError("copy source must be a regular directory")
    entries = [(source, Path())]
    for parent, directories, files in os.walk(source, followlinks=False):
        for name in [*directories, *files]:
            path = Path(parent) / name
            if is_link(path) or not (path.is_file() or path.is_dir()):
                raise PreparationError("copy source contains a link or special node: " + str(path))
            entries.append((path, path.relative_to(source)))
    return entries


def safe_destination(path):
    for candidate in (Path(path), *Path(path).parents):
        if is_link(candidate):
            raise PreparationError("destination traverses an existing link: " + str(candidate))


def physical_entry(path):
    """Resolve parent aliases (including macOS /tmp), preserving the final node.

    The explicit destination root is a caller-selected location. Links found
    inside that root are still rejected by safe_destination before any copy.
    """
    path = Path(path).absolute()
    return path.parent.resolve() / path.name


def copy_tree(source, destination, *, source_fingerprint=None, expected_fingerprint=None, dry_run=False):
    if bool(source_fingerprint) != bool(expected_fingerprint):
        raise PreparationError("cache copy requires both fingerprint paths")
    if source_fingerprint and not cache_compatible(source_fingerprint, expected_fingerprint):
        return {"status": "skipped", "reason": "cache fingerprint mismatch or unavailable; run import"}
    source, destination = physical_entry(source), physical_entry(destination)
    if same_path(source, destination) or inside_real_root(source, destination) or inside_real_root(destination, source):
        raise PreparationError("copy source and destination overlap")
    entries = checked_tree(source)
    # Complete the conflict check before copying the first file. This helper
    # never merges conflicting content, deletes extras, or follows links.
    for origin, relative in entries:
        target = destination / relative
        safe_destination(target)
        if target.exists():
            if (origin.is_dir() and not target.is_dir()) or (origin.is_file() and not target.is_file()):
                raise PreparationError("destination type conflicts: " + str(target))
            if origin.is_file() and origin.read_bytes() != target.read_bytes():
                raise PreparationError("destination content conflicts: " + str(target))
    if not dry_run:
        for origin, relative in entries:
            target = destination / relative
            safe_destination(target)
            if is_link(origin) or not (origin.is_file() or origin.is_dir()):
                raise PreparationError("copy source changed to a link or special node")
            if origin.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif target.exists():
                if not target.is_file() or origin.read_bytes() != target.read_bytes():
                    raise PreparationError("destination changed during copy: " + str(target))
            else:
                # Exclusive create also protects a destination appearing after
                # the preflight. Partial output can be retried without overwrite.
                with origin.open("rb") as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                shutil.copystat(origin, target, follow_symlinks=False)
    return {"status": "planned" if dry_run else "copied", "entries": len(entries)}


def link_directory(source, destination, *, create_source=False, dry_run=False):
    source, destination = physical_entry(source), physical_entry(destination)
    if is_link(source) or (source.exists() and not source.is_dir()) or (not source.exists() and not create_source):
        raise PreparationError("link source must be an existing directory (or use --create-source)")
    if is_link(destination):
        if same_path(destination, source):
            if not source.exists():
                if dry_run:
                    return {"status": "planned"}
                safe_destination(source)
                source.mkdir(parents=True, exist_ok=True)
            return {"status": "unchanged"}
        raise PreparationError("existing link has another target")
    if os.path.lexists(destination):
        raise PreparationError("destination exists; no file or directory was replaced")
    if same_path(source, destination) or inside_real_root(destination, source) or inside_real_root(source, destination):
        raise PreparationError("link source and destination overlap")
    safe_destination(destination)
    if dry_run:
        return {"status": "planned"}
    if create_source:
        safe_destination(source)
        source.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # CPython's native junction primitive avoids cmd.exe and shell quoting,
        # and does not require Windows symbolic-link privileges.
        import _winapi
        _winapi.CreateJunction(str(source), str(destination))
    else:
        destination.symlink_to(source, target_is_directory=True)
    if not same_path(destination, source):
        raise PreparationError("created link target could not be verified")
    return {"status": "linked"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--executable", action="append", default=[])
    check.add_argument("--file", action="append", default=[])
    check.add_argument("--cwd", help="Base directory for relative input paths")
    for name in ("copy", "link"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True)
        command.add_argument("--destination", required=True)
        command.add_argument("--dry-run", action="store_true")
        if name == "copy":
            command.add_argument("--source-fingerprint")
            command.add_argument("--expected-fingerprint")
        else:
            command.add_argument("--create-source", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            result = preflight(args.executable, args.file, cwd=args.cwd)
        elif args.command == "copy":
            result = copy_tree(args.source, args.destination, source_fingerprint=args.source_fingerprint,
                               expected_fingerprint=args.expected_fingerprint, dry_run=args.dry_run)
        else:
            result = link_directory(args.source, args.destination, create_source=args.create_source, dry_run=args.dry_run)
    except (PreparationError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
