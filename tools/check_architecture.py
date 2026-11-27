"""Check deployment and Python dependency boundaries without running product code."""

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins/chef"
PACKAGE = PLUGIN / "chef"


def inspect(plugin=PLUGIN):
    plugin = Path(plugin)
    package = plugin / "chef"
    errors = []
    for path in sorted(plugin.rglob("*.py")):
        relative = path.relative_to(plugin)
        if path.name.startswith("test_") or path.name == "conftest.py":
            errors.append(f"test in deployment tree: {relative}")
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        if not path.is_relative_to(package):
            if relative.as_posix() != "codex/sync_shared.py" and any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for node in ast.walk(tree)
            ):
                errors.append(f"implementation in registration tree: {relative}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).startswith("sys.path."):
                errors.append(f"implementation changes import paths: {relative}:{node.lineno}")
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
                if node.level:
                    errors.append(f"use explicit chef imports: {relative}:{node.lineno}")
            else:
                continue
            for name in imports:
                if name.split(".")[0] in {"skills", "hooks", "codex", "grok", "pytest", "tests"}:
                    errors.append(
                        f"implementation imports a registration/test tree: {relative}:{node.lineno}: {name}"
                    )
                if relative.parts[1] == "infrastructure" and name.startswith(
                    ("chef.slots", "chef.cli", "chef.dashboard", "chef.mission.state")
                ):
                    errors.append(
                        f"infrastructure imports an application entry point: {relative}:{node.lineno}: {name}"
                    )
                if relative.parts[1] == "verification" and name.startswith("chef.slots"):
                    errors.append(f"verification imports the slots application: {relative}:{node.lineno}")
    return errors


def main():
    errors = inspect() + inspect_versions()
    for error in errors:
        print(error)
    if errors:
        return 1
    print("Deployment tests, package imports, infrastructure dependencies, and release versions verified")
    return 0


def inspect_versions(repo=REPO):
    """Native installers use separate manifests; keep all release pins equal."""
    repo = Path(repo)
    versions = {}
    try:
        marketplace = json.loads((repo / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        versions["marketplace"] = next(item["version"] for item in marketplace["plugins"] if item["name"] == "chef")
        for runtime in ("claude", "codex", "grok"):
            manifest = repo / "plugins/chef" / ("." + runtime + "-plugin/plugin.json")
            versions[runtime] = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        hermes = (repo / "plugins/chef/hermes/plugin.yaml").read_text(encoding="utf-8")
        version = re.search(r"(?m)^version:[ \t]*(\S+)[ \t]*$", hermes)
        versions["hermes"] = version.group(1).strip("\"'") if version else None
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        return ["release version metadata cannot be read: " + str(error)]
    if any(not isinstance(value, str) or not value for value in versions.values()):
        return ["release versions must be nonempty strings"]
    if len(set(versions.values())) != 1:
        return ["release versions differ: " + ", ".join(name + "=" + value for name, value in versions.items())]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
