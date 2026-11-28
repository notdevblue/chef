"""Exercise the replacement CLI when lifecycle tests request a legacy marker.

This helper runs both the real pre-tool hook and the separately executed CLI;
tests that inspect host denial call the hook alone instead.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

from chef.guards.mission import MARKER, render_output, Outcome
from chef.runtimes.hooks import normalize_payload


def run_lifecycle_hook(script, value, *, env=None, cwd=None, timeout=30):
    runtime = "codex" if "codex" in str(script) else ""
    normalized = normalize_payload(value, runtime)
    command = (normalized.get("tool_input") or {}).get("command", "")
    match = MARKER.fullmatch(command.strip())
    argv = None
    if match and normalized.get("session_id") and normalized.get("han_runtime") != "hermes":
        argv = [sys.executable, "-B", str(Path(script).resolve().parent.parent / "mission_control.py"), match[1], match[2],
                "--runtime", normalized["han_runtime"], "--session", normalized["session_id"],
                "--project", normalized["cwd"], "--request", uuid.uuid4().hex]
        command = " ".join('"' + item + '"' if " " in item else item for item in argv)
        value = dict(value)
        key = "toolInput" if "toolInput" in value else "tool_input"
        value[key] = {**value.get(key, {}), "command": command}
    result = subprocess.run([sys.executable, "-B", str(script)], input=json.dumps(value).encode(),
                            capture_output=True, env=env, cwd=cwd, timeout=timeout)
    if argv and result.returncode == 0:
        output = json.loads(result.stdout or b"{}")
        if output.get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
            executed = subprocess.run(argv, capture_output=True, env=env, cwd=normalized["cwd"], timeout=timeout)
            if executed.returncode == 0:
                messages = json.loads(executed.stdout)["messages"]
                result.stdout = json.dumps(render_output(Outcome(messages=messages))).encode()
            else:
                error = json.loads(executed.stderr)["error"]
                result.stdout = json.dumps(render_output(Outcome(deny=error))).encode()
    return result
