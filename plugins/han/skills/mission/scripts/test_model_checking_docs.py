"""Model-checking workflow packaging and cross-runtime instruction contracts.

These tests validate shipped guidance, not TLC or a project's formal model.
"""
from pathlib import Path
import json
import re
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMMON = ROOT / "skills/mission/references/model-checking-workflow.md"
CODEX = ROOT / "codex/skills/han-mission/references/model-checking-workflow.md"


def test_shared_model_workflow_is_packaged_identically():
    assert COMMON.read_bytes() == CODEX.read_bytes()


@pytest.mark.parametrize("skill", ["skills/mission", "codex/skills/han-mission"])
def test_mission_connects_model_to_whole_lifecycle(skill):
    path = ROOT / skill / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert (path.parent / "references/model-checking-workflow.md").is_file()
    for marker in ("## 동작 모델 검사", "PoC 생략은 모델 검사 생략이 아니다",
                   "담당 구현·원자적 처리 경계", "모델 누락 / 구현 이탈 / 모델 밖 문제",
                   "모델 또는 회귀 테스트", "별도 승인 단계·역할은 추가하지 않는다"):
        assert marker in text


def test_grok_uses_common_workflow_and_existing_roles():
    text = (ROOT / "grok/skills/han-mission/SKILL.md").read_text(encoding="utf-8")
    assert "references/model-checking-workflow.md" in text
    assert "고정 모델 실행은 기존 verifier" in text


def test_workflow_has_limits_evidence_and_fail_closed_outcomes():
    text = COMMON.read_text(encoding="utf-8")
    for marker in ("안전성", "진행성", "도달 가능성", "탐색 미완료",
                   "종료 코드 0만으로 pass 처리하지 않는다", "라인·브랜치 커버리지 측정이 아니다",
                   "규칙 id → 모델 속성/전이 → 구현 위치·담당 → 테스트·결과",
                   "도구가 같고 결과 근거가 유효하면", "판독은 내장되어 있지 않다",
                   "held/미확인", "기록 감사·사람 승인은 그대로 유지"):
        assert marker in text


def test_new_relative_markdown_links_resolve():
    paths = [ROOT / "skills/mission/SKILL.md", ROOT / "codex/skills/han-mission/SKILL.md",
             ROOT / "skills/mission/references/verification-workflow.md"]
    for path in paths:
        for link in re.findall(r"\]\(([^)]+model-checking-workflow\.md|model-checking-workflow\.md)\)", path.read_text(encoding="utf-8")):
            assert (path.parent / link).is_file(), (path, link)


def test_edited_role_toml_remains_parseable_and_connected():
    for name in ("han-developer-small", "han-developer-medium", "han-verifier"):
        data = tomllib.loads((ROOT / f"codex/agents/{name}.toml").read_text(encoding="utf-8"))
        assert "model-checking-workflow.md" in data["developer_instructions"]


def test_shared_release_versions_match():
    claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    grok = json.loads((ROOT / ".grok-plugin/plugin.json").read_text())
    market = json.loads((ROOT.parents[1] / ".claude-plugin/marketplace.json").read_text())
    assert claude["version"] == grok["version"] == market["plugins"][0]["version"]
