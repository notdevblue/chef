"""slots / contracts."""

import re

from chef.locations import PLUGIN_ROOT

HOOKS_DIR = str(PLUGIN_ROOT / "hooks")


OK = 0


FAILED = 1


USAGE = 2


BUSY = 3


TERMINAL_RUNTIMES = ("claude", "codex", "grok")


RUNTIMES = (*TERMINAL_RUNTIMES, "hermes")


ROW_FIELDS = ("name", "path", "client", "created", "synced", "state")


NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


MISSION_FIELD = re.compile(r"^(상태|현재 단계):[ \t]*(.*?)\s*$")


KEEP_DESCRIPTION = "han slot keep-local {name}"


SYNC_STEP = "sync"


LAUNCH_PREPARE = "prepare"


STATE_CREATING = "creating"


STATE_READY = "ready"


STATE_FAILED = "failed:"


STATE_REFRESH_FAILED = "failed:refresh:"


PREVIEW_LIMIT = 20


SESSION_NAME_LIMIT = 40


RESUME_PROMPT = "미션 이어서"


RUNTIME_PROBE_LINES = 50


CLAUDE_ENTRY_TYPES = (
    "user",
    "assistant",
    "system",
    "summary",
    "file-history-snapshot",
    "queue-operation",
    "progress",
)
