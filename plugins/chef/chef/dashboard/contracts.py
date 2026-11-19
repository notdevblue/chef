"""dashboard / contracts."""

import re

from chef.guards import mission as mission_guard
from chef.locations import PLUGIN_ROOT
from chef.mission import state as mission_state

SKILLS_DIR = str(PLUGIN_ROOT / "skills")


DEFAULT_PORT = 8765


REFRESH_SECONDS = 30


MAIN_LABEL = "main"


RUNNING = "진행"


APPROVED = mission_state.APPROVED


SKIPPED = mission_state.SKIPPED


SKELETON_HEADING = re.compile(r"^##\s.*뼈대", re.M)


CONFIRM_LETTERS = "ABC"


IDLE_SECONDS = 10 * 60


SUBAGENT_STALE_SECONDS = 60 * 60


HANDBACK_TOOL = "SubagentHandback"


SUBAGENT_FILE = re.compile(r"agent-([\w-]+)\.jsonl")


CODEX_ROLLOUT = re.compile(
    r"rollout-.+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl"
)


TASK_NOTIFICATION = re.compile(r"<task-id>([\w-]+)</task-id>")


ACTIVITY_TYPES = ("user", "assistant", "system", "api-request", "event_msg", "response_item")


TRANSCRIPT_TAIL_BYTES = 256 * 1024


CODEX_TURN_DONE = "task_complete"


CODEX_TURN_EVENTS = ("task_started", "user_message", CODEX_TURN_DONE)


STAGE_TOKEN = re.compile(r"^[A-Za-z]+")


MARKED_URL = "https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"


QUESTION_NAME = re.compile(r"(?:ask-\d+|confirm-[ABC]\d*)\.md")


QUESTION_RECORD_HEADER = re.compile(r"^문서: `[^`]+` · 개정: \d+\s*$", re.M)


MAX_TEXT_BYTES = 4 * 1024 * 1024


BINARY_PROBE_BYTES = 8192


INSPECTION_SETTLE_NS = 2 * 10**9


NO_INSPECTION = ((), ())


HTML_TYPE = "text/html; charset=utf-8"


TEXT_TYPE = "text/plain; charset=utf-8"


IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


JS_TYPE = "text/javascript; charset=utf-8"


ASSET_TYPES = {
    ".js": JS_TYPE,
    ".mjs": JS_TYPE,
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


UTF8_SUFFIXES = ("json", "javascript", "xml")


RAW_PREFIX = "/raw/"


READ_ERRORS = (OSError, ValueError, mission_state.StateError)


FILE_URL = re.compile(r"file:///([^\s)>\]\"'`]*)")


NATURAL_SPLIT = re.compile(r"(\d+)")


DOC_FIRST = ("mission.md", "report.md")


SYSTEM_DOCUMENTS = {
    "mission.md",
    mission_state.UNITS_FILE,
    mission_state.LEDGER_FILE,
    mission_state.DECISIONS_FILE,
    mission_guard.SESSIONS_FILE,
}


TABLES = (
    ("마일스톤", mission_state.UNITS_FILE, mission_state.UNIT_HEADER),
    ("판정 기록", mission_state.LEDGER_FILE, mission_state.LEDGER_HEADER),
    ("결정", mission_state.DECISIONS_FILE, mission_state.DECISIONS_HEADER),
    ("세션", mission_guard.SESSIONS_FILE, mission_guard.SESSIONS_HEADER),
)
