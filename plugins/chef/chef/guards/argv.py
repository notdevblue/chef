"""Describe direct process arguments without interpreting data as shell code."""

import base64
import os
import re
import shlex
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Invocation:
    argv: tuple
    shell: str = ""
    script: str = ""
    child: tuple = ()


def render(argv):
    """Legacy regex view only. Execution and structural checks keep the argv."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def wrapper_child(name, arguments):
    """Find forwarded argv without interpreting quoted argument data as code."""
    value_options = {
        "env": {"-u", "--unset", "-C", "--chdir"},
        "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from", "-T", "--command-timeout", "-D", "--chdir", "-R", "--chroot"},
        "timeout": {"-s", "--signal", "-k", "--kill-after"},
        "xargs": {"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L", "-l", "-n", "-P", "-s", "--eof", "--replace", "--max-lines", "--max-args", "--max-procs", "--max-chars"},
        "nohup": set(), "command": set(),
    }
    if name not in value_options:
        return ()
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            index += 1
            break
        if not value.startswith("-"):
            if name == "env" and re.match(r"[A-Za-z_]\w*=", value):
                index += 1
                continue
            break
        if name == "env" and (value in ("-S", "--split-string") or value.startswith(("-S", "--split-string="))):
            raise ValueError("env split-string requires an explicit inspectable command")
        if (name == "env" and (value.startswith("-C") or value in ("--chdir",) or value.startswith("--chdir="))) or (
            name == "sudo" and (value.startswith(("-D", "-R")) or value in ("--chdir", "--chroot") or value.startswith(("--chdir=", "--chroot=")))
        ):
            raise ValueError("wrapper working-directory changes are not supported; use command.cwd")
        index += 2 if value in value_options[name] else 1
    if name == "timeout":
        index += 1  # duration
    return tuple(arguments[index:])


def describe(argv):
    if (
        not isinstance(argv, (list, tuple)) or not argv
        or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
        or not argv[0]
    ):
        raise ValueError("a nonempty executable and NUL-free argv are required")
    name = os.path.basename(argv[0]).lower().removesuffix(".exe")
    if name in ("sh", "bash", "zsh", "dash", "ksh", "fish"):
        index = 1
        command_string = False
        while index < len(argv):
            arg = argv[index]
            if arg in ("--", "-", "+"):
                index += 1
                break
            if not arg.startswith(("-", "+")):
                break
            if arg in ("--rcfile", "--init-file"):
                index += 2
                continue
            if not arg.startswith("--"):
                options = arg[1:]
                command_string |= "c" in options
                # POSIX shells keep parsing invocation options after -c. The
                # script is the first operand after all options (or after one
                # -- delimiter), not necessarily the next argv element.
                index += 2 if "o" in options or "O" in options else 1
            else:
                index += 1
        if command_string:
            if index >= len(argv):
                raise ValueError("shell -c requires a command string")
            return Invocation(tuple(argv), "Bash", argv[index])
    elif name in ("powershell", "pwsh"):
        for index, arg in enumerate(argv[1:], 1):
            flag = arg.lower()
            if flag in ("-file", "-f", "--"):
                break
            if flag in ("-command", "-c", "-commandwithargs"):
                if index + 1 >= len(argv):
                    raise ValueError("PowerShell command option requires a command")
                script = argv[index + 1] if flag == "-commandwithargs" else " ".join(argv[index + 1:])
                return Invocation(tuple(argv), "PowerShell", script)
            if flag in ("-encodedcommand", "-enc", "-ec", "-e"):
                if index + 1 >= len(argv):
                    raise ValueError("PowerShell encoded command requires a value")
                try:
                    script = base64.b64decode(argv[index + 1], validate=True).decode("utf-16-le").lstrip("\ufeff")
                except (ValueError, UnicodeError) as error:
                    raise ValueError("cannot inspect PowerShell encoded command") from error
                return Invocation(tuple(argv), "PowerShell", script)
    elif name == "cmd":
        for index, arg in enumerate(argv[1:], 1):
            if arg.lower() in ("/c", "/k"):
                if index + 1 >= len(argv):
                    raise ValueError("cmd requires a command string")
                return Invocation(tuple(argv), "PowerShell", " ".join(argv[index + 1:]))
    return Invocation(tuple(argv), child=wrapper_child(name, argv[1:]))
