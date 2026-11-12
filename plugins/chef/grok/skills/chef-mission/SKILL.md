---
name: chef-mission
description: "Grok CLI에서 작업을 단계와 마일스톤으로 나눠 조사·구현·검증·독립 리뷰를 파견하고 확인 A·B·C를 받는다. 미션 진행·이어서 하기·중단·상태 또는 chef-mission 요청에 사용한다. S는 미션 없이 처리하고 M·L은 코디네이터로 진행한다."
---

# Grok mission

실행 전에 [공통 절차](../../../shared/skills/mission.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## Grok 실행 계약

공통 참조 정본은 `<plugin-root>/skills/mission/references/`다.


- 어댑터 검색은 `.grok/orchestrator` → `.claude/orchestrator` → `.codex/orchestrator` → `HAN_ADAPTERS_DIR` → 저장소 `adapters`다. 검색·병합의 정본은 [공통 어댑터 계약](../../../skills/mission/references/adapter-schema.md)의 Grok 절과 `--runtime grok` 실행 결과다.
  `adapter → adapter.runtimes.grok → local → local.runtimes.grok` 순으로 적용한다. 잘못 설정된 경로를 다른 어댑터로 우회하지 않는다.
- `check_human_doc.py` 등 공통 스크립트는 `<plugin-root>/skills/mission/scripts/` 아래 실제 경로로 호출한다.
  공통 문서의 p4 래퍼 경로는 `<plugin-root>/grok/skills/chef-mission/scripts/p4c.py`로 대체한다. 이 래퍼가 `runtime="grok"`을 공유 코드에 전달한다.
  `guard.p4Client.require`가 켜져 있으면 p4 쓰기는 이 Grok 래퍼로 한다. 런타임을 받는 다른 CLI도 `--runtime grok`을 쓴다.
- 파견·수집은 [Grok 역할](references/grok-roles.md)의 native 도구만 쓴다. 현재 선택된 native Grok 4.7 모델을 상속하고 코디네이터의 effort는 바꾸지 않는다. 도구가 없으면 미확인으로 반환하며 직접 구현·외부 봇 인계로 우회하지 않는다. 역할·모델·depth 1과 작은 리뷰 라우팅은 이 역할 계약을 따른다.
- `/chef:adapter-init`·Codex adapter-init 호출은 Grok `chef-adapter-init`, 슬롯 호출은 `chef-slots`, `../distill/SKILL.md`는 `<plugin-root>/grok/skills/chef-distill/SKILL.md`로 대체한다.
  어댑터의 프로젝트 스킬은 실제 설치 파일과 도구를 읽고 적용한다. 필요한 스킬·Unity 연결이 없으면 해당 단계를 미확인으로 남긴다.
  Claude JSONL 전용 사용량 집계기는 Grok 로그에 적용하지 않는다. 실제 모델·effort 증거와 미확인 한계만 기록한다.

## 실행 경로

- `<mission-entry>`: `<plugin-root>/grok/scripts/mission_entry.py`, `<mission-entry-runtime>`: 빈 값.
- `<mission-state>`: `<plugin-root>/skills/mission/scripts/mission_state.py`, `<mission-state-runtime>`: `--runtime grok --project "<작업 루트>"`.
- `<p4-wrapper>`: `<plugin-root>/grok/skills/chef-mission/scripts/p4c.py`.
- `<distill-skill>`: `<plugin-root>/grok/skills/chef-distill/SKILL.md`. 슬롯은 `chef-slots`, 어댑터는 `chef-adapter-init`이다.
- M·L은 코디네이터이며 프로젝트 파일은 역할에 맡긴다. S는 `chef-run` 직접 처리이며 사용자가 명시적으로 원할 때만 `init --tier S --force-tier`로 미션을 열고 분리를 유지한다.
- Grok 세션 id는 실제 세션에서 확인한다. open·resume 단독 마커 뒤 훅 안내와 ACTIVE의 미션 id·현재 세션 id를 모두 확인한다.
