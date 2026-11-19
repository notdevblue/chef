"""runtimes / claude / session."""

import os

from chef.infrastructure.paths import normalize


def project_dir_name(path):
    return "".join(
        char if char.isascii() and char.isalnum() else "-" * (2 if ord(char) > 0xFFFF else 1)
        for char in str(path)
    )


def claude_memory_dir(project_path):
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return normalize(os.path.join(home, "projects", project_dir_name(project_path), "memory"))
