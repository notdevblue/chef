---
name: slots
description: 미션을 동시에 여러 개 돌리기 위한 슬롯 워크스페이스를 만들고, 이어받고, 최신으로 갱신하고, 상태를 보고, 미션 세션을 띄우고, 지운다. "슬롯 만들어줘", "슬롯 상태", "슬롯 갱신", "슬롯에서 미션 돌려줘", "슬롯 지워줘", "미션 대시보드", "/chef:slots" 요청에 사용한다. 슬롯 조작에는 slots 설정이 필요하며 미션 대시보드는 슬롯 없이도 동작한다.
---

# Claude slots

실행 전에 [공통 절차](../../shared/skills/slots.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 두 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<slots-script>`: `<plugin-root>/skills/slots/scripts/slots.py`, `<slots-runtime>`: 빈 값.
- `<dashboard-script>`: `<plugin-root>/skills/slots/scripts/dashboard.py`, `<dashboard-runtime>`: 빈 값.
- 어댑터 검색·병합은 [공통 어댑터 계약](../mission/references/adapter-schema.md)의 Claude 절을 따른다.
- 새 탭은 `claude "/chef:mission <작업>"`, 재개는 `claude "/chef:mission 미션 이어서"`다.
  `run --resume`의 `--new-session`과 런타임 인계는 아래 계약을 따른다.
  `ACTIVE.session`이 다른 런타임(Codex) 세션이거나 기록을 못 찾으면 거부한다. 사용자가 이 런타임으로 넘겨받겠다고 하면
  `--new-session`을 붙여 기존 세션을 열지 않고 새 세션에서 `미션 이어서`를 시작한다(새 세션의 미션 절차가 다른 창에서 아직 도는지 확인받음).
- 슬롯 세션은 `.claude/settings.local.json`(MCP 승인 등)과 개인 `_My/CLAUDE.md`, 프로젝트 자동 메모리(메인의 메모리 폴더를
  가리키는 junction)를 메인과 같게 받는다(`adapter.md`의 "슬롯 준비"). 새 폴더에서 처음 `claude`를 띄우면 폴더 신뢰
  확인 창이 한 번 뜨니 사용자가 승인해야 한다. 이 스킬은 전역 설정 파일을 건드리지 않는다.
- auto mode가 `--discard-local`을 막으면 사용자가 `!`로 직접 실행하도록 안내한다.
