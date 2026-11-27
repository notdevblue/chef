"""One approved invocation shared by independently installed guard participants.

This broker only coordinates an exception after each caller's hard protections.
Participants are trusted hook identities, never names taken from tool input.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from chef.guards.adapters import valid_mission_id
from chef.guards.argv import wrapper_child
from chef.guards.contracts import MARKER, P4_TOKEN
from chef.guards.shell import command_fragments, split_fragments
from chef.infrastructure.locking import FileLock
from chef.infrastructure.storage import atomic_write_bytes, atomic_write_json, make_private_directory
from chef.runtimes.hooks import normalize_payload


SHARED_ALLOW_ONCE_VERSION = 1


class SharedAllowOnceError(ValueError):
    """An approval cannot safely be shared; callers must deny the invocation."""


def configured_participants(adapter):
    """None preserves legacy consumption; malformed opt-in fails closed."""
    guard = adapter.get("guard", {})
    if not isinstance(guard, dict):
        raise SharedAllowOnceError("guard must be an object")
    if "allowOnceParticipants" not in guard:
        return None
    participants = guard["allowOnceParticipants"]
    if (
        not isinstance(participants, list) or len(participants) < 2
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item) for item in participants)
        or len(set(participants)) != len(participants)
        or "chef-mission" not in participants
    ):
        raise SharedAllowOnceError("guard.allowOnceParticipants requires distinct hook names including chef-mission")
    return participants


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def shared_once_context(adapter):
    """Return the canonical effective adapter every participant must supply.

    Each hook must load this adapter with Chef's loader for the same runtime;
    a separately constructed projection or a different runtime overlay differs.
    """
    try:
        copied = json.loads(json.dumps(adapter, sort_keys=True, allow_nan=False))
        files = copied.get("guard", {}).get("allowOnceAuthorityFiles", [])
        if (
            not isinstance(files, list) or len(files) > 8
            or any(not isinstance(path, str) or not os.path.isabs(path) for path in files)
        ):
            raise SharedAllowOnceError("guard.allowOnceAuthorityFiles requires at most eight absolute file paths")
        paths = [_path(path) for path in files]
        if len(set(paths)) != len(paths):
            raise SharedAllowOnceError("guard.allowOnceAuthorityFiles contains duplicate files")
        authorities = [{"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()} for path in paths]
        return {"adapter": copied, "authorityFiles": authorities}
    except (OSError, ValueError, TypeError) as error:
        raise SharedAllowOnceError(f"shared allow-once authority context failed: {error}") from error


def normalize_shared_payload(payload):
    """Normalize native shell aliases and their effective invocation directory."""
    payload = dict(payload)
    tool = payload.get("tool_name")
    if tool in ("exec_command", "functions.exec_command", "functions.exec", "exec"):
        payload["tool_name"] = "Bash"
    original_input = payload.get("tool_input")
    if not isinstance(original_input, dict):
        return normalize_payload(payload)
    tool_input = dict(original_input)
    if "command" in tool_input and "cmd" in tool_input and tool_input["command"] != tool_input["cmd"]:
        raise SharedAllowOnceError("shared allow-once has conflicting command aliases")
    if "command" not in tool_input and "cmd" in tool_input:
        tool_input["command"] = tool_input["cmd"]
    payload["tool_input"] = tool_input
    payload = normalize_payload(payload)
    if payload.get("tool_name") in ("Bash", "PowerShell"):
        cwd = tool_input.get("workdir") or tool_input.get("cwd")
        if cwd:
            if not isinstance(cwd, str):
                raise SharedAllowOnceError("shared allow-once has an invalid effective working directory")
            if not os.path.isabs(cwd):
                base = payload.get("cwd")
                if not isinstance(base, str) or not os.path.isabs(base):
                    raise SharedAllowOnceError("shared allow-once has no absolute working-directory base")
                cwd = os.path.join(base, cwd)
            payload.setdefault("han_origin_cwd", payload.get("cwd") or os.getcwd())
            payload["cwd"] = _path(cwd)
            tool_input = dict(payload["tool_input"])
            tool_input.pop("cwd", None)
            tool_input["workdir"] = payload["cwd"]
            payload["tool_input"] = tool_input
    return payload


def _path(value):
    return os.path.normcase(os.path.realpath(os.path.abspath(value)))


def _matching_line(data, wanted):
    # Keep all bytes of every unconsumed line, including BOM and line endings.
    offset = 0
    for line in data.splitlines(keepends=True):
        text = line.decode("utf-8-sig" if offset == 0 else "utf-8").strip()
        if text == wanted:
            remaining = data[:offset] + data[offset + len(line):]
            if offset == 0 and data.startswith(b"\xef\xbb\xbf") and remaining:
                remaining = b"\xef\xbb\xbf" + remaining
            return remaining
        offset += len(line)
    return None


def _call_id(payload):
    values = [payload[key] for key in ("tool_use_id", "tool_call_id", "call_id") if key in payload]
    if not values:
        raise SharedAllowOnceError("shared allow-once requires a native tool call id")
    if any(not isinstance(value, str) or not value.strip() for value in values) or len(set(values)) != 1:
        raise SharedAllowOnceError("shared allow-once has invalid or conflicting native tool call ids")
    return values[0]


def _context(state_dir, mission_id, command, payload, active, participants, config):
    payload = normalize_shared_payload(payload)
    if (
        active.corrupt or active.id != mission_id or not valid_mission_id(mission_id)
        or not active.session or not active.opened_at
        or payload.get("session_id") != active.session
        or any(payload.get(key) for key in ("agent_id", "subagentType", "han_worker", "han_hermes_worker"))
    ):
        raise SharedAllowOnceError("shared allow-once requires the current mission owner, not a worker")
    runtime = payload.get("han_runtime")
    if runtime not in ("claude", "codex", "grok", "hermes") or (active.runtime and runtime != active.runtime):
        raise SharedAllowOnceError("shared allow-once runtime does not match ACTIVE")
    tool = payload.get("tool_name")
    if tool not in ("Bash", "PowerShell"):
        raise SharedAllowOnceError("shared allow-once requires a shell tool")
    wanted = command.strip()
    if (
        not wanted or "\x00" in wanted or len(wanted.splitlines()) != 1
        or len(split_fragments(wanted, tool)) != 1
        or len(command_fragments(wanted, tool)) != 1
        or MARKER.match(wanted)
    ):
        raise SharedAllowOnceError("shared allow-once requires one single-line command without a mission marker")
    tokens = [token.strip("\"'") for token in P4_TOKEN.findall(wanted)]
    if tokens:
        wrapper_child(os.path.basename(tokens[0]).lower().removesuffix(".exe"), tokens[1:])
    tool_input = payload.get("tool_input") or {}
    shell_family = "powershell" if tool == "PowerShell" else "bash"
    shell = tool_input.get("shell")
    if shell:
        if not isinstance(shell, str):
            raise SharedAllowOnceError("shared allow-once has an invalid shell")
        executable = shell.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        declared = "powershell" if executable in ("pwsh", "powershell") else "bash" if executable in ("bash", "sh", "zsh", "dash", "ksh") else ""
        if declared != shell_family:
            raise SharedAllowOnceError("shared allow-once shell family is unsupported or conflicts with its tool")
    supplied = tool_input.get("command")
    if not isinstance(supplied, str) or supplied.strip() != wanted:
        raise SharedAllowOnceError("shared allow-once command does not match the native payload")
    cwd = tool_input.get("workdir") or tool_input.get("cwd") or payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise SharedAllowOnceError("shared allow-once requires an effective working directory")
    if not os.path.isabs(cwd):
        base = payload.get("cwd")
        if not isinstance(base, str) or not os.path.isabs(base):
            raise SharedAllowOnceError("shared allow-once has no absolute working-directory base")
        cwd = os.path.join(base, cwd)
    return {
        "version": SHARED_ALLOW_ONCE_VERSION,
        "stateDir": _path(state_dir), "mission": mission_id,
        "owner": active.session, "openedAt": active.opened_at,
        "runtime": runtime, "callId": _call_id(payload),
        "cwd": _path(cwd), "tool": "shell",
        "shell": shell_family,
        "command": wanted,
        "config": _digest({"participants": participants, "adapter": config}),
    }


def consume_shared_allow_once(state_dir, mission_id, command, *, payload, participant, participants, active, config=None):
    """Consume once, then acknowledge each configured hook once for this call.

    False means no matching approval or receipt. Other unsafe states raise
    SharedAllowOnceError. Pass the same effective adapter as ``config`` in every
    hook. Token replacement precedes receipt publication: a crash can lose an
    approval but cannot restore a consumed approval or authorize another call.
    """
    try:
        preflight = (config or {}).get("adapter", {}).get("guard", {}).get("allowOncePreflightCommand")
        token = Path(state_dir) / mission_id / "allow-once"
        if participant == "chef-mission" and preflight and token.is_file() and _matching_line(token.read_bytes(), command.strip()) is not None:
            from chef.guards.policy import command_denial

            reason = command_denial(preflight, payload, payload.get("cwd"))
            if reason:
                raise SharedAllowOnceError("shared allow-once preflight denied without consuming approval: " + reason)
        return _consume(state_dir, mission_id, command, payload, participant, participants, active, config)
    except SharedAllowOnceError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise SharedAllowOnceError(f"shared allow-once check failed: {error}") from error


def _consume(state_dir, mission_id, command, payload, participant, participants, active, config):
    from chef.cli.mission_entry import active_lock
    from chef.guards.mission import read_active

    configured = configured_participants({"guard": {"allowOnceParticipants": participants}})
    if participant not in configured:
        raise SharedAllowOnceError("shared allow-once participant is not configured")
    if not valid_mission_id(mission_id) or not isinstance(command, str):
        raise SharedAllowOnceError("shared allow-once has invalid mission or command")
    folder = Path(state_dir) / mission_id
    token = folder / "allow-once"
    receipts = folder / "allow-once.receipts"
    # Unrelated ordinary commands must not require a call id or create state.
    if not token.exists() and not receipts.exists():
        return False
    owner_lock = active_lock(state_dir)
    owner_lock.timeout = 2
    with owner_lock, FileLock(folder / "allow-once.shared.lock", "shared allow-once", timeout=2):
        if read_active(state_dir) != active:
            raise SharedAllowOnceError("shared allow-once ACTIVE changed")
        ids_present = any(key in payload for key in ("tool_use_id", "tool_call_id", "call_id"))
        # Command/config/cwd are deliberately excluded from the address so an
        # id replay with modified arguments finds and rejects the old receipt.
        receipt = None
        if ids_present:
            normalized = normalize_shared_payload(payload)
            identity = {
                "stateDir": _path(state_dir), "mission": mission_id,
                "owner": active.session, "openedAt": active.opened_at,
                "runtime": normalized.get("han_runtime"), "callId": _call_id(normalized),
            }
            receipt = receipts / (_digest(identity) + ".json")
        if receipt is not None and receipt.exists():
            context = _context(state_dir, mission_id, command, payload, active, configured, config)
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise SharedAllowOnceError("shared allow-once receipt is malformed")
            if record.get("context") != context:
                raise SharedAllowOnceError("shared allow-once call context changed")
            consumed = record.get("participants")
            if (
                not isinstance(consumed, list) or not consumed
                or any(item not in configured for item in consumed)
                or len(set(consumed)) != len(consumed)
            ):
                raise SharedAllowOnceError("shared allow-once receipt is malformed")
            if participant in consumed:
                raise SharedAllowOnceError("shared allow-once participant already consumed this call")
            record["participants"] = consumed + [participant]
            atomic_write_json(receipt, record, durable=True)
            return True
        data = token.read_bytes() if token.exists() else b""
        remaining = _matching_line(data, command.strip())
        if remaining is None:
            return False
        context = _context(state_dir, mission_id, command, payload, active, configured, config)
        # The atomic replacement is authoritative consumption, even if writing
        # the receipt fails. Never roll it back on a receipt publication error.
        atomic_write_bytes(token, remaining, durable=True)
        make_private_directory(receipts)
        atomic_write_json(receipt, {"context": context, "participants": [participant]}, durable=True)
        return True
