"""guards / contracts."""

import re
from dataclasses import dataclass, field
from typing import NamedTuple

from chef.locations import PLUGIN_ROOT

GUARDED_TOOLS = ("Bash", "PowerShell")


WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


SUPPORTED_SCHEMA = 1


ADAPTER_RELATIVE = (".claude", "orchestrator", "adapter.json")


REGISTRY_FILE = "projects.json"


REGISTRY_ENV = "HAN_ADAPTERS_DIR"


REPO_ENV = "HAN_REPO"


USER_CONFIG_ENV = "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR"


LOCAL_FILE = "local.json"


LOG_ENV = "HAN_GUARD_LOG"


LOG_FIELDS = ("session_id", "agent_id", "agent_type", "tool_name", "cwd", "hook_event_name")


LOG_COMMAND_LENGTH = 120


SESSIONS_FILE = "sessions.tsv"


SESSIONS_HEADER = ("at", "action", "session", "transcript")


SESSION_ACTIONS = ("open", "resume")


CELL_BREAKS = re.compile(r"[\t\r\n]+")


MAX_DEPTH = 6


STALE_HOURS_DEFAULT = 24


STALE_ENV = "HAN_STALE_HOURS"


HEARTBEAT_SECONDS = 5 * 60


STALE_SCAN_LIMIT = 5000


FORCE_CLOSE_HINT = "닫으려면 사용자 확인 후 mission_state.py close-stale --force-close-stale"


P4_EXECUTABLES = ("p4", "p4.exe")


P4_VALUE_OPTIONS = ("-b", "-c", "-C", "-d", "-F", "-H", "-L", "-p", "-P", "-Q", "-r", "-u", "-v", "-x", "-z")


P4_WRITE_COMMANDS = frozenset(
    (
        "add",
        "edit",
        "delete",
        "copy",
        "merge",
        "integrate",
        "integ",
        "move",
        "rename",
        "reconcile",
        "rec",
        "resolve",
        "revert",
        "submit",
        "shelve",
        "unshelve",
        "sync",
        "flush",
        "clean",
        "undo",
        "lock",
        "unlock",
        "change",
        "changelist",
        "open",
        "populate",
        "obliterate",
    )
)


P4_READ_FLAG_COMMANDS = ("change", "changelist")  # -o 는 spec 출력만


P4_TOKEN = re.compile(r"""(?:"[^"]*"|'[^']*'|\S+)""")


P4C_NAME = "p4c.py"


P4C_SCRIPT = str(PLUGIN_ROOT / "skills/mission/scripts/p4c.py")


P4C_CALL = re.compile(
    r"""^(?:(?:"[^"]*[\\/]|'[^']*[\\/]|\S*[\\/])?(?:python3?|py)(?:\.exe)?["']?(?:\s+-\S+)*\s+)?"""
    r"""(?:"[^"]*p4c\.py"|'[^']*p4c\.py'|\S*p4c\.py)(?=\s|$)\s*(.*)$""",
    re.IGNORECASE | re.DOTALL,
)


P4C_OWN_VALUE_OPTIONS = ("--project", "--client")


P4C_OWN_FLAGS = ("--which", "--dry-run")


MARKER = re.compile(
    r"""^(?:echo|Write-Output|Write-Host)\s+["']?han-mission\s+(open|resume|close)\s+(\S+?)["']?\s*$""",
    re.IGNORECASE,
)


MISSION_ID = re.compile(r"\w[\w.\-]*")


MARKER_ALONE_REASON = (
    "마커(han-mission open|resume|close)는 단독 호출로. 다른 명령과 섞으면 처리하지 않고 막음"
)


CONTINUATIONS = {
    "Bash": re.compile(r"\\\r?\n"),
    "PowerShell": re.compile(r"`\r?\n"),
}


ESCAPE_CHARS = {"Bash": "\\", "PowerShell": "`"}


CHAIN_PREFIX = re.compile(
    r"^(?:&\s*|\$?\(\s*|\{\s*|!\s*|(?:do|then|else|if|while|until|time|exec)\s+)+",
    re.IGNORECASE,
)


BASH_ASSIGN = re.compile(r"^(?:export\s+)?([A-Za-z_]\w*)=(.*)$", re.DOTALL)


POWERSHELL_ASSIGN = re.compile(r"^\$(?:env:)?([A-Za-z_]\w*)\s*=\s*(.*)$", re.DOTALL)


SIMPLE_VALUE = re.compile(r"""^(?:"[^"]*"|'[^']*'|\S*)$""")


FIRST_TOKEN_REST = re.compile(r"""^(?:"[^"]*"|'[^']*'|\S+)\s*(.*)$""", re.DOTALL)


VARIABLE_REFERENCE = re.compile(r"""(["']?)(?:\$\{(\w+)\}|\$env:(\w+)|\$(\w+))\1""")


COMMAND_SUBSTITUTION = ("$(", "`")


QUOTED_TEXT = re.compile(r"""\"[^\"]*\"|'[^']*'""")


BASH_GROUP_OPENER = re.compile(r"[$<>]\(")


POWERSHELL_GROUP_OPENER = re.compile(r"\(")


POWERSHELL_SUBEXPRESSION_OPENER = re.compile(r"\$\(")


BACKTICK_BODY = re.compile(r"`([^`]*)`")


WRAPPER_BODIES = (
    r"xargs(?:\s+-\S+)*",
    r"cmd(?:\.exe)?\s+/[ck]",
    r"env(?:\s+-\S+)*(?:\s+\w+=\S+)*",
    r"nohup",
    r"sudo(?:\s+-\S+)*",
    r"start(?:\s+/\w+)*",
    r"(?:powershell|pwsh)(?:\.exe)?(?:\s+-\w+(?:\s+[^-\s]\S*)?)*?\s+-c(?:ommand)?",
    r"(?:ba|da|z)?sh(?:\.exe)?(?:\s+-\w+)*?\s+-\w*c",
    r"eval",
    r"command",
    r"timeout\s+\S+",
)


WRAPPERS = tuple(
    re.compile(r"^(?:\S*[\\/])?" + body + r"(?=\s|$)\s*", re.IGNORECASE) for body in WRAPPER_BODIES
)


class Active(NamedTuple):
    id: str
    session: str
    opened_at: str
    corrupt: bool = False
    runtime: str = ""


NO_ACTIVE = Active("", "", "")


CORRUPT_ACTIVE = Active("-", "", "", True)


class Staleness(NamedTuple):
    stale: bool
    last: float  # 마지막 활동 epoch 초, 0 이면 모름
    hours: float  # 마지막 활동 뒤 지난 시간
    limit: float  # 끊김 기준 시간, 0 이면 끔


NOT_STALE = Staleness(False, 0.0, 0.0, 0.0)


class Verdict(NamedTuple):
    kind: str
    reason: str = ""
    marker: object = None

    @property
    def denied(self):
        return self.kind in ("rule", "mixed")


@dataclass
class Outcome:
    deny: str = ""
    messages: list = field(default_factory=list)


LITERAL_ARGUMENT = r'(?:[A-Za-z0-9_./:\\-]+|"[A-Za-z0-9_./:\\ -]+"|\'[A-Za-z0-9_./:\\ -]+\')'


LITERAL_COMMAND = re.compile(
    r"(?P<name>rg(?:\.exe)?|grep(?:\.exe)?|echo|Write-Output)(?P<args>(?:[ \t]+" + LITERAL_ARGUMENT + r")+)",
    re.IGNORECASE,
)


LITERAL_SEARCH_OPTIONS = frozenset(
    ("-n", "-i", "-l", "-F", "-E", "--line-number", "--no-heading", "--fixed-strings", "--no-config")
)
