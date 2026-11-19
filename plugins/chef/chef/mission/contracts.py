"""mission / contracts."""

import os
import re
from dataclasses import dataclass

from chef.locations import PLUGIN_ROOT

SKILLS_DIR = str(PLUGIN_ROOT / "skills")


HOOKS_DIR = str(PLUGIN_ROOT / "hooks")


OK = 0


FAILED = 1


TEMPLATES_DIR = os.path.join(SKILLS_DIR, "mission", "references", "templates")


TEMPLATE_FILES = ("mission.md", "units.tsv", "ledger.tsv", "decisions.tsv", "report.md")


ID_TEMPLATES = ("mission.md", "report.md")


ID_PLACEHOLDER = "<id>"


UNITS_FILE = "units.tsv"


LEDGER_FILE = "ledger.tsv"


DECISIONS_FILE = "decisions.tsv"


SHARED_DOCUMENTS_FILE = "shared-documents.tsv"


UNIT_HEADER = (
    "id",
    "goal",
    "files",
    "done",
    "verify",
    "timebox",
    "forbid",
    "human",
    "after",
    "unit",
    "started",
    "status",
)


LEDGER_HEADER = ("at", "unit", "status", "grade", "evidence", "files")


DECISIONS_HEADER = ("at", "id", "what", "chosen", "why", "affects")


SHARED_DOCUMENTS_HEADER = ("path", "sha256", "at")


SYSTEM_DOCUMENTS = {
    "mission.md",
    UNITS_FILE,
    LEDGER_FILE,
    DECISIONS_FILE,
    "sessions.tsv",
    SHARED_DOCUMENTS_FILE,
}


BRIEF_FIELDS = ("goal", "files", "done", "verify", "timebox", "forbid")


EDITABLE_FIELDS = BRIEF_FIELDS + ("human", "after")


UNIT_STATUSES = ("todo", "doing", "done", "held")


LEDGER_STATUSES = ("pass", "fail", "retry", "blocked")


MISSION_STATES = ("진행", "중단", "닫힘")


NEEDS_HUMAN = "needs-human"


NONE_CELL = "-"


VERDICT_LIMIT = 4


RUNTIMES = ("claude", "codex", "grok")


UNIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


CONFIRM_KEY = re.compile(r"[A-Za-z]\d*")


SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


DECISION_ID = re.compile(r"D(\d+)")


HEADER_FIELD = re.compile(r"^(상태|현재 단계|PoC|확인 [A-Za-z]\d*):[ \t]*(.*?)[ \t]*$")


INSERTABLE_KEYS = ("PoC",)


HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


QUESTION_FILE = re.compile(r"^(?:confirm|ask)-.+\.md$")


CONFIRM_FILE = re.compile(r"^confirm-([A-Za-z])(\d*)\.md$")


ASK_FILE = re.compile(r"^ask-.+\.md$")


ANSWER_DATE = re.compile(r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b")


APPROVED = "승인"


SKIPPED = "생략"


class StateError(Exception):
    pass


@dataclass
class Context:
    adapter: dict
    state_dir: str
    mission_id: str
    runtime: str
    project: str

    @property
    def folder(self):
        return os.path.join(self.state_dir, self.mission_id)

    def path(self, name):
        return os.path.join(self.folder, name)


MISSION_TIERS = {
    "S": "한두 파일·단일 수정. 미션 대상 아님(직접 처리하거나 한 번 파견)",
    "M": "여러 파일·한 레이어 이상, 검증 게이트 1개 이상",
    "L": "여러 레이어·PoC·사람 확인이 필요한 다단계 작업",
}
