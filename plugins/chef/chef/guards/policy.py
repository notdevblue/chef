"""Synchronous external policy checks shared by workers and mission transitions."""

import json
import subprocess
import tempfile

MISSION_POLICY_API = 1
POLICY_TIMEOUT = 8
POLICY_OUTPUT_LIMIT = 65536


def command_denial(argv, payload, cwd, *, env=None, timeout=POLICY_TIMEOUT, messages=None):
    """Run trusted argv without a shell; empty output or explicit hook allow permits."""
    messages = {"error": "Policy command could not complete: ", "failed": "Policy command failed: ",
                "json": "Policy command output is not JSON", "denied": "Policy command denied: ", **(messages or {})}
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n") for value in argv)):
        return "Policy command must be a nonempty string argv array"
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(argv, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                    cwd=cwd, env=env, stdout=stdout, stderr=stderr, timeout=timeout, shell=False)
            if stdout.tell() > POLICY_OUTPUT_LIMIT or stderr.tell() > POLICY_OUTPUT_LIMIT:
                return "Policy command output exceeds the byte limit"
            if result.returncode:
                stderr.seek(0)
                detail = stderr.read(POLICY_OUTPUT_LIMIT).decode("utf-8", "replace").strip()[-500:]
                return messages["failed"] + (detail or f"exit code {result.returncode}")
            stdout.seek(0)
            text = stdout.read(POLICY_OUTPUT_LIMIT).decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as error:
        return messages["error"] + str(error)
    if not text:
        return ""
    try:
        output = json.loads(text)
    except ValueError:
        return messages["json"]
    specific = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    if (isinstance(specific, dict) and specific.get("permissionDecision") == "allow"
            and "decision" not in output):
        return ""
    reason = ""
    if isinstance(specific, dict):
        reason = str(specific.get("permissionDecisionReason") or "")
    if isinstance(output, dict) and not reason:
        reason = str(output.get("reason") or "")
    return messages["denied"] + (reason or "no explicit allow decision")


def mission_policy_configured(adapter):
    settings = adapter.get("guard")
    return isinstance(settings, dict) and "missionPolicyCommand" in settings


def mission_policy_denial(adapter, payload):
    if not mission_policy_configured(adapter):
        return ""
    return command_denial(adapter["guard"]["missionPolicyCommand"], payload, payload.get("cwd"), timeout=POLICY_TIMEOUT)
