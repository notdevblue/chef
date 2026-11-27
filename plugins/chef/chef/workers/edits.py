"""Validate Grok's proposed edits before trusted, journalled file application."""

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from chef.infrastructure.paths import inside_real_root, is_link
from chef.infrastructure.fingerprints import execution_digest as implementation_digest
from chef.infrastructure.storage import atomic_write_bytes, atomic_write_json, replace_file

MAX_CONTENT = 4 * 1024 * 1024
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "changes"],
          "properties": {"summary": {"type": "string"}, "changes": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["path", "beforeSha256", "content"],
              "properties": {"path": {"type": "string"}, "beforeSha256": {"type": ["string", "null"]},
                             "content": {"type": ["string", "null"]}}}}}}


def path_for(cwd, name):
    root = Path(cwd).resolve()
    if not isinstance(name, str) or not name or "\x00" in name or Path(name).is_absolute():
        raise ValueError("edit path must be a relative assigned file")
    path = root / name
    if not inside_real_root(root, path):
        raise ValueError("edit path escaped the workspace")
    current = root
    for part in path.relative_to(root).parts:
        if part in (".", "..") or part.casefold() in (".git", ".chef", ".claude", ".codex", ".grok", ".hermes"):
            raise ValueError("edit path traverses protected metadata")
        current /= part
        if is_link(current):
            raise ValueError("edit path may not traverse a filesystem link")
    if path.exists() and not path.is_file():
        raise ValueError("edit target must be a regular file")
    return path


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def snapshot(cwd, names):
    return {name: file_hash(path_for(cwd, name)) for name in names}


def authorize(data, cwd, path, content):
    from chef.guards import mission
    from chef.workers import context

    owner = data["owner"]
    context_path = data.get("contextPath")
    if context_path:
        context_file = Path(context_path)
        current = json.loads(context_file.read_text(encoding="utf-8"))
        cancel_path = Path(data.get("cancelPath") or context_file.parent / "cancel.json")
        if current.get("revoked") or cancel_path.exists():
            raise ValueError("edit application cancelled or revoked")
        if data.get("digest") != implementation_digest():
            raise ValueError("edit application implementation changed")
    active = mission.read_active(owner["state"])
    if (active.corrupt or active.id != owner["mission"] or active.session != owner["session"]
            or active.runtime not in ("", owner["runtime"])):
        raise ValueError("edit application lost parent ownership")
    payload = {"hook_event_name": "PreToolUse", "session_id": owner["session"],
               "han_runtime": owner["runtime"], "cwd": str(cwd), "agent_id": "chef-grok-worker",
               "tool_name": "Write", "tool_input": {"file_path": str(path), "content": content or "",
                                                       "operation": "delete" if content is None else "write"},
               "han_worker": data}
    denied = context.denial(payload) or mission.decide(payload).deny
    if denied:
        raise ValueError("proposed edit guard denied: " + denied)


def write_preserving_mode(path, content, mode):
    """Publish through a sibling whose original mode is restored before replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".worker-" + uuid.uuid4().hex + ".tmp")
    try:
        atomic_write_bytes(staged, content, durable=True, creation_mode=mode if mode is not None else 0o666)
        if mode is not None:
            staged.chmod(mode)
        replace_file(staged, path)
    finally:
        if staged.exists():
            staged.unlink()


def apply(text, *, cwd, before, data, directory):
    """Validate the entire response before any edit; retain partial-write evidence."""
    value = json.loads(text)
    if (not isinstance(value, dict) or set(value) != {"summary", "changes"}
            or not isinstance(value["summary"], str) or not isinstance(value["changes"], list)
            or len(value["changes"]) > len(before)):
        raise ValueError("invalid proposed edit response")
    checked = []
    seen = set()
    for change in value["changes"]:
        if not isinstance(change, dict) or set(change) != {"path", "beforeSha256", "content"}:
            raise ValueError("invalid proposed edit")
        name, content = change["path"], change["content"]
        if not isinstance(name, str) or name not in before or name in seen:
            raise ValueError("proposed edit is duplicate or outside assigned files")
        seen.add(name)
        path = path_for(cwd, name)
        if content is not None and (not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CONTENT):
            raise ValueError("invalid proposed file content")
        if content is None and before[name] is None:
            raise ValueError("cannot delete a nonexistent assigned file")
        if change["beforeSha256"] != before[name] or file_hash(path) != before[name]:
            raise ValueError("proposed edit preimage changed")
        if path.exists() and (not path.stat().st_mode & 0o222 or not os.access(path, os.W_OK)):
            raise PermissionError("edit target is read-only; use the project checkout procedure first")
        authorize(data, cwd, path, content)
        checked.append((name, path, content.encode("utf-8") if content is not None else None))
    journal = {"schema": 1, "status": "prepared", "before": before, "applied": [],
               "proposed": value["changes"]}
    journal_path = Path(directory) / "apply-journal.json"
    atomic_write_json(journal_path, journal, durable=True)
    for name, path, content in checked:
        authorize(data, cwd, path, content.decode("utf-8") if content is not None else None)
        path = path_for(cwd, name)
        if file_hash(path) != before[name]:
            raise ValueError("edit preimage changed immediately before application")
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        if path.exists() and (not mode & 0o222 or not os.access(path, os.W_OK)):
            raise PermissionError("edit target became read-only")
        if content is None:
            path.unlink()
        else:
            write_preserving_mode(path, content, mode)
        journal["applied"].append({"path": name, "sha256": file_hash(path)})
        atomic_write_json(journal_path, journal, durable=True)
    journal["status"] = "completed"
    atomic_write_json(journal_path, journal, durable=True)
    return value["summary"]
