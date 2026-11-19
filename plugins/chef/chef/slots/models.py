"""slots / models."""

import os
from dataclasses import dataclass, field


@dataclass
class Step:
    id: str
    command: str | list[str]
    cwd: str
    group: int
    exclusive: bool
    ok_exit: list
    log: str
    out: str


@dataclass
class StepResult:
    id: str
    code: int
    seconds: float
    ok: bool
    out: str
    log: str

    def failure_text(self):
        paths = [path for path in (self.out, self.log) if os.path.isfile(path)]
        return f"{self.id} 종료 코드 {self.code} — 로그 {', '.join(paths) or self.out}"


@dataclass
class Outcome:
    name: str
    state: str
    detail: str = ""
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def failed(self):
        return self.state == "failed"
