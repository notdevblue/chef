"""Advisory plan diagnostics and opt-in coordinator execution policy.

An adapter allowlist is an operator's trust declaration, not a proof that a
program is read-only. Tool pins detect changes to the executable, detected
launcher script and explicitly pinned dependencies; they cannot discover every
imported dependency or side effect.
Ordinary preflight, guards, execution and receipt validation remain required.
"""

import os
import re
from pathlib import Path

from chef.verification.contracts import VerificationError, file_hash


def diagnostic(code, message, field=None, command_index=None):
    result = {"code": code, "message": message}
    if field is not None:
        result["field"] = field
    if command_index is not None:
        result["commandIndex"] = command_index
    return result


def plan_warnings(project, plan):
    """Return advisory diagnostics for a validated plan without changing it.

    Missing step declarations retain their existing execution/reuse semantics.
    Only whole-project paths/globs are called broad; this is not a dependency
    completeness analysis. timeoutSeconds is a deadline, never elapsed duration.
    """
    from chef.verification.plan import project_path

    warnings = []
    root = Path(project).resolve()

    def broad(value):
        text = value.replace(os.sep, "/")
        while text.startswith("./"):
            text = text[2:]
        if any(char in text for char in "*?["):
            return any(char in text.split("/")[0] for char in "*?[")
        return project_path(project, value) == root

    if any(broad(value) for value in plan["inputs"]):
        warnings.append(diagnostic("plan_inputs_broad", "Plan inputs include the whole project.", "inputs"))
    for index, command in enumerate(plan["commands"]):
        if "reusable" not in command:
            warnings.append(diagnostic(
                "reuse_policy_missing", "Declare reusable explicitly for this command.", "reusable", index))
        if "inputs" not in command:
            warnings.append(diagnostic(
                "command_inputs_missing", "Command inputs default to all plan inputs.", "inputs", index))
        elif any(broad(value) for value in command["inputs"]):
            warnings.append(diagnostic(
                "command_inputs_broad", "Command inputs include the whole project.", "inputs", index))
        if "outputs" not in command:
            warnings.append(diagnostic(
                "command_outputs_missing", "Declare outputs explicitly; use [] for a read-only check.", "outputs", index))
        elif any(broad(value) for value in command["outputs"]):
            warnings.append(diagnostic(
                "command_outputs_broad", "Command outputs include the whole project.", "outputs", index))
    return warnings


def trusted_commands(project, policy):
    """Validate one exact ordered command sequence and its required tool pins."""
    from chef.verification.plan import project_path

    if not isinstance(policy, dict) or set(policy) != {"grade", "commands"}:
        raise ValueError("Each coordinator check needs only grade and commands.")
    if not isinstance(policy["grade"], str) or not policy["grade"]:
        raise ValueError("Coordinator check grade must be explicit.")
    if not isinstance(policy["commands"], list) or not policy["commands"]:
        raise ValueError("Coordinator check commands must be a nonempty list.")
    commands = []
    for command in policy["commands"]:
        if not isinstance(command, dict) or set(command) != {"argv", "cwd", "tools"}:
            raise ValueError("Trusted commands need exactly argv, cwd and tools.")
        argv = command["argv"]
        if (
            not isinstance(argv, list) or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or any(any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value) for value in argv)
        ):
            raise ValueError("Trusted argv must contain nonempty strings without control characters.")
        cwd = project_path(project, command["cwd"])
        pins = command["tools"]
        if not isinstance(pins, dict) or not pins:
            raise ValueError("Trusted commands need SHA256 pins for every runtime tool.")
        tools = {}
        for value, sha256 in pins.items():
            if (
                not isinstance(value, str) or not value
                or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)
                or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            ):
                raise ValueError("Tool pins need a path and lowercase SHA256.")
            path = str((Path(project).resolve() / value).resolve())
            if path in tools:
                raise ValueError("Tool pins contain duplicate resolved paths.")
            tools[path] = sha256
        commands.append({"argv": argv, "cwd": str(cwd), "tools": tools})
    return commands


def coordinator_tool_identity(project, policies):
    """Bind declared dependency contents to receipt/reuse identity.

    Invalid opt-in policy still permits the ordinary verifier route. Missing or
    unreadable tools get explicit markers here; direct eligibility rejects them.
    """
    try:
        if not isinstance(policies, list):
            return {}
        commands = [command for policy in policies for command in trusted_commands(project, policy)]
    except (VerificationError, OSError, ValueError, TypeError, RuntimeError):
        return {}
    identities = {}
    for path in sorted({path for command in commands for path in command["tools"]}):
        try:
            identities[path] = file_hash(path)
        except OSError:
            identities[path] = "unreadable" if Path(path).exists() else "missing"
    return identities


def coordinator_eligibility(project, plan, settings=None):
    """Classify a complete plan; missing/unsupported policy always uses verifier.

    verify.coordinatorChecks is a list of {grade, commands} records. Each commands
    value is an entire ordered sequence of {argv, cwd, tools}; tools maps resolved
    executable/launcher and dependency paths (project-relative or absolute) to
    SHA256 hashes. Every detected runtime tool must be pinned; extra dependency
    pins are allowed and checked too. Exact argv, resolved cwd and all tool hashes
    must match one record. Undeclared imports remain the policy author's concern.
    This function reads files but never launches commands or creates receipts.
    """
    from chef.verification.plan import (
        command_runtime, project_path, reference_identity, validate_plan, verification_settings,
    )

    def routed(*reasons):
        return {"eligible": not reasons, "route": "verifier" if reasons else "coordinator", "reasons": list(reasons)}

    if not isinstance(settings, dict) or "coordinatorChecks" not in settings:
        return routed(diagnostic("policy_missing", "No explicit coordinator check policy is configured."))
    policies = settings["coordinatorChecks"]
    try:
        if not isinstance(policies, list) or not policies:
            raise ValueError("coordinatorChecks must be a nonempty list.")
        trusted = [(policy, trusted_commands(project, policy)) for policy in policies]
    except (VerificationError, OSError, ValueError, TypeError, RuntimeError) as error:
        return routed(diagnostic("policy_invalid", str(error)))
    grades = settings.get("grades")
    if (
        not isinstance(grades, list) or not grades
        or not all(isinstance(grade, str) and grade for grade in grades)
        or not isinstance(plan, dict) or plan.get("grade") not in grades
    ):
        return routed(diagnostic("grade_not_allowed", "The plan grade must be explicitly allowed by verify.grades."))
    try:
        validate_plan(project, plan, grades, require_matches=True)
        reference_identity(project, plan)
        configured = verification_settings(project, plan, settings)
        if configured["resources"] or configured["readiness"]:
            return routed(diagnostic("preparation_required", "Resource or readiness checks require the verifier route."))
        warnings = plan_warnings(project, plan)
        if warnings:
            return routed(*warnings)
        if plan.get("results", []):
            return routed(diagnostic("result_artifacts", "Plans with result artifacts require the verifier route."))
        commands = []
        for index, command in enumerate(plan["commands"]):
            if set(command) - {"id", "argv", "cwd", "timeoutSeconds", "reusable", "inputs", "outputs"}:
                return routed(diagnostic("command_unsupported", "Unknown command options require the verifier route.", command_index=index))
            if command["outputs"] != []:
                return routed(diagnostic("outputs_declared", "Only commands with explicit empty outputs can run directly.", command_index=index))
            runtime = command_runtime(project, command)
            executable = Path(runtime["executable"])
            launcher = executable.stem.lower()
            if launcher in {
                "sh", "bash", "zsh", "dash", "ash", "ksh", "csh", "tcsh", "fish", "powershell", "pwsh", "cmd",
            }:
                return routed(diagnostic(
                    "shell_requires_verifier", "Shell commands require the verifier route regardless of script pins.", command_index=index))
            if executable.suffix.lower() in {".bat", ".cmd"} or (
                len(runtime["tools"]) == 1
                and (launcher.startswith(("python", "pypy")) or launcher in {
                    "node", "nodejs",
                })
            ):
                return routed(diagnostic(
                    "launcher_unpinned", "A directly identifiable launcher script is required for interpreter commands.", command_index=index))
            commands.append({
                "argv": command["argv"],
                "cwd": str(project_path(project, command.get("cwd", "."))),
                "tools": runtime["tools"],
            })
        for policy, allowed in trusted:
            if policy["grade"] != plan["grade"] or len(allowed) != len(commands):
                continue
            if all(
                expected["argv"] == actual["argv"] and expected["cwd"] == actual["cwd"]
                and all(expected["tools"].get(path) == sha256 for path, sha256 in actual["tools"].items())
                and all(file_hash(path) == sha256 for path, sha256 in expected["tools"].items())
                for expected, actual in zip(allowed, commands)
            ):
                return routed()
        return routed(diagnostic("command_identity_mismatch", "The entire ordered command sequence and tool pins must match one trusted check."))
    except (VerificationError, OSError, ValueError, TypeError, RuntimeError) as error:
        return routed(diagnostic("plan_ineligible", str(error)))


def require_coordinator_eligible(project, plan, settings=None):
    """Reject direct execution unless the current complete plan is allowlisted.

    Callers must still perform normal preflight/guard and verification execution;
    eligibility grants no grade, result, receipt, review or completion exemption.
    """
    result = coordinator_eligibility(project, plan, settings)
    if not result["eligible"]:
        raise VerificationError(
            "; ".join(reason["message"] for reason in result["reasons"]), "coordinator_ineligible")
    return result
