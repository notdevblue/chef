"""Launch a CLI with the current Python available to its installed Chef hooks."""

import argparse
import shutil
import subprocess

from chef.infrastructure.python import python_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=("claude", "codex", "grok", "hermes"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    executable = shutil.which(args.runtime)
    if not executable:
        parser.error(f"{args.runtime} executable is not available on PATH")
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    try:
        return subprocess.call([executable, *arguments], env=python_environment())
    except (OSError, ValueError) as error:
        parser.error(str(error))
