"""Recognize bounded read operations without mistaking argument data for code."""

import os
import re
import shlex

from chef.guards.shell import command_fragments, unquote


def tokens(command):
    values = shlex.split(command, posix=False)
    if values and values[0] == "&":
        values.pop(0)
    return [unquote(value) for value in values]


def readonly(command, tool="Bash"):
    try:
        masked = re.sub(r'''"[^"\r\n]*"|'[^'\r\n]*' ''', "", command, flags=re.VERBOSE)
        if any(char in masked for char in (">", "<", "&", "%", "(", ")")):
            return False
        fragments = command_fragments(command, tool)
        return bool(fragments) and all(read_fragment(fragment) for fragment in fragments)
    except (ValueError, TypeError):
        return False


def read_fragment(fragment):
    values = tokens(fragment)
    if not values or any(value in (">", ">>", "2>", "2>>", "*>", "*>>") for value in values):
        return False
    # Shell substitution is executable even when it is inside a quoted argument.
    if "$(" in fragment or "`" in fragment:
        return False
    name = re.split(r"[\\/]", values[0])[-1].casefold().removesuffix(".exe")
    args = values[1:]
    if name in ("rg", "grep"):
        return not any(value.split("=", 1)[0] in ("--pre", "--hostname-bin")
                       or (value.startswith(("\\", "-")) and "\\" in value) for value in args)
    if name in ("cat", "head", "tail", "pwd", "ls", "get-content", "get-item", "get-childitem", "test-path",
                "select-string", "select-object", "get-filehash", "write-output", "echo"):
        return not any(char in fragment for char in (";", "&", ">", "<", "{" , "}"))
    if name == "sed":
        return len(args) >= 2 and args[0] == "-n" and bool(re.fullmatch(r"\d+(?:,\d+|,\$)?p", args[1]))
    if name == "certutil":
        return len(args) in (2, 3) and args[0].casefold() == "-hashfile"
    if name == "git":
        return (bool(args) and args[0] in ("status", "diff", "show", "log", "rev-parse", "ls-files", "ls-tree")
                and not any(value.split("=", 1)[0] in ("--output", "--ext-diff", "--exec", "--config-env") for value in args))
    return False


def control_invocation(command, tool="Bash"):
    """Return actual control script invocations, never filenames inside search data."""
    result = []
    for fragment in command_fragments(command, tool):
        try:
            values = tokens(fragment)
        except ValueError:
            continue
        if not values:
            continue
        executable = re.split(r"[\\/]", values[0])[-1].casefold()
        if re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", executable):
            values = values[1:]
            while values and values[0].startswith("-"):
                option = values.pop(0)
                if option in ("-c", "-m"):
                    values = []
                    break
                if option in ("-X", "-W") and values:
                    values.pop(0)
        if not values:
            continue
        name = re.split(r"[\\/]", values[0])[-1].casefold()
        if name in ("mission_state.py", "mission_entry.py", "mission_control.py",
                    "worker.py", "chef_cli.py", "han-mission", "chef-mission"):
            result.append((values[0], values[1:]))
        elif name in ("echo", "write-output") and len(values) > 1 and values[1] in ("han-mission", "chef-mission"):
            result.append((values[1], values[2:]))
    return result


def owned_worker_command_denial(payload, command, tool):
    """Match compact worker/review options to the session reported by the host."""
    if "--session" not in command and "--job" not in command:
        return ""
    for script, argv in control_invocation(command, tool):
        options = argv[:argv.index("--")] if "--" in argv else argv
        flags = {value.split("=", 1)[0] for value in options if value.startswith("--")}
        name = re.split(r"[\\/]", script)[-1].casefold()
        worker = options if name == "worker.py" else []
        if name == "chef_cli.py" and options[:1] == ["worker"]:
            worker = options[1:]
        if worker[:1] == ["--home"]:
            worker = worker[2:]
        elif worker and worker[0].startswith("--home="):
            worker = worker[1:]
        if not ((worker[:1] == ["dispatch"] and "--session" in flags)
                or (name == "chef_cli.py" and options[:1] == ["review"] and "--job" in flags)):
            continue
        for flag, expected in (("--session", payload.get("session_id")),
                               ("--runtime", payload.get("han_runtime", "claude"))):
            supplied = []
            for index, value in enumerate(options):
                if value == flag:
                    supplied.append(options[index + 1] if index + 1 < len(options) else "")
                elif value.startswith(flag + "="):
                    supplied.append(value.split("=", 1)[1])
            if not expected or supplied != [expected]:
                return f"Chef worker/review command requires {flag} of this host session"
    return ""
