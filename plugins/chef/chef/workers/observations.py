"""Observed runtime identity, with bounded reads and local evidence pointers."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from chef.infrastructure.paths import is_link, same_path
from chef.infrastructure.storage import atomic_write_json

MAX_ENTRIES = 10000
MAX_ROLLOUT_BYTES = 8 * 1024 * 1024
MAX_META_BYTES = 64 * 1024


def text(value):
    if isinstance(value, str) and value.strip() and value.lower() not in ("inherit", "unknown"):
        return value
    return None


def stream_identity(runtime, stdout):
    """Use runtime metadata events only, never assistant prose or requested config."""
    observed = {}
    for index, line in enumerate(stdout.splitlines(), 1):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if runtime == "claude" and event.get("type") == "system" and event.get("subtype") == "init":
            model = text(event.get("model"))
            effort = text(event.get("effort") or event.get("reasoning_effort") or event.get("perTurnEffort"))
            if model:
                observed.update(observedModel=model, modelLine=index)
            if effort:
                observed.update(observedEffort=effort, effortLine=index)
        elif runtime == "grok" and event.get("type") == "end":
            usage = event.get("modelUsage")
            if isinstance(usage, dict) and len(usage) == 1:
                model = text(next(iter(usage)))
                if model:
                    observed.update(observedModel=model, modelLine=index)
    return observed


def rollout_paths(root, session):
    """Visit at most MAX_ENTRIES nodes in the selected profile, newest dirs first."""
    pending = [root]
    visited = 0
    while pending and visited < MAX_ENTRIES:
        directory = pending.pop()
        children = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        return
                    path = Path(entry.path)
                    if is_link(path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if len(path.relative_to(root).parts) <= 3:
                            children.append(path)
                    elif entry.is_file(follow_symlinks=False) and session in entry.name and entry.name.endswith(".jsonl"):
                        yield path
        except OSError:
            continue
        pending.extend(sorted(children))


def codex_identity(session, cwd, environment):
    """Read only a matching rollout whose metadata proves session and workspace."""
    try:
        if str(uuid.UUID(session)) != session:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    if not home.is_absolute():
        home = Path(cwd) / home
    root = home.resolve() / "sessions"
    for path in rollout_paths(root, session):
        try:
            with path.open("rb") as stream:
                head = stream.readline(MAX_META_BYTES + 1)
                if len(head) > MAX_META_BYTES:
                    continue
                metadata = json.loads(head)
                payload = metadata.get("payload", {})
                if (metadata.get("type") != "session_meta" or payload.get("id") != session
                        or not isinstance(payload.get("cwd"), str) or not same_path(payload["cwd"], cwd)):
                    continue
                size = os.fstat(stream.fileno()).st_size
                offset = max(len(head), size - MAX_ROLLOUT_BYTES)
                stream.seek(offset)
                if offset > len(head):
                    stream.readline(MAX_ROLLOUT_BYTES)
                    offset = stream.tell()
                raw = stream.read(MAX_ROLLOUT_BYTES)
            latest = None
            for line in raw.splitlines(keepends=True):
                try:
                    event = json.loads(line)
                except ValueError:
                    offset += len(line)
                    continue
                current = event.get("payload", {}) if isinstance(event, dict) else {}
                if not isinstance(event, dict):
                    offset += len(line)
                    continue
                if event.get("type") == "turn_context" and isinstance(current, dict):
                    if current.get("cwd") and (not isinstance(current["cwd"], str) or not same_path(current["cwd"], cwd)):
                        offset += len(line)
                        continue
                    model = text(current.get("model"))
                    effort = text(current.get("reasoning_effort") or current.get("effort"))
                    latest = {"source": str(path), "sourceOffset": offset,
                              "sourceLineSha256": hashlib.sha256(line).hexdigest(), "session_id": session}
                    if model:
                        latest["observedModel"] = model
                    if effort:
                        latest["observedEffort"] = effort
                offset += len(line)
            if latest:
                return latest
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


def collect(runtime, stdout, session, *, cwd, environment, directory):
    """Persist reported identity only. Missing evidence leaves fields absent."""
    try:
        return collect_available(runtime, stdout, session, cwd=cwd, environment=environment, directory=directory)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {}


def collect_available(runtime, stdout, session, *, cwd, environment, directory):
    if runtime == "codex":
        observed = codex_identity(session, cwd, environment)
        if not observed:
            return {}
        path = Path(directory) / "runtime-observation.json"
        atomic_write_json(path, {"schema": 1, **observed}, durable=True)
        evidence = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    else:
        observed = stream_identity(runtime, stdout)
        if not observed:
            return {}
        path = Path(directory) / "work.stdout.jsonl"
        if not path.is_file():
            return {}
        evidence = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for key in ("modelLine", "effortLine"):
            if key in observed:
                evidence[key] = observed[key]
    result = {key: observed[key] for key in ("observedModel", "observedEffort") if key in observed}
    return {**result, "runtimeEvidence": evidence} if result else {}
