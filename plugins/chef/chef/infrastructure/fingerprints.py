"""Identify the complete implementation, including extracted dependencies."""

import hashlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def implementation_digest(root=None, *, excludes=()):
    root = Path(root) if root is not None else PACKAGE_ROOT
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if any(path.is_relative_to(root / name) for name in excludes):
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def execution_digest(root=None):
    """Presentation-only dashboard updates do not revoke running role work."""
    return implementation_digest(root, excludes=("dashboard",))
