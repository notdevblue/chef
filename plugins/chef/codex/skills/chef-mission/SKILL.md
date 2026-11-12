---
name: chef-mission
description: Codex CLI에서 작업을 단계와 마일스톤으로 나눠 조사·구현·검증·리뷰를 파견하고 사람 확인 A·B·C를 받는다. 미션 진행·이어서 하기·중단 또는 $chef:chef-mission 요청에 사용한다. 프로젝트 어댑터가 필요하며 한두 파일짜리 작은 수정에는 쓰지 않는다.
---

# Codex mission

실행 전에 [공통 절차](../../../shared/skills/mission.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## Codex 실행 계약


- `references/codex-roles.md`를 읽고 사용 가능한 서브에이전트 도구로 역할별 생성·결과 수집·후속 지시를 한다.
  등록되지 않은 역할 이름을 도구에 넘기지 않는다. research와 심층 review는 부모 설정을 상속한다.
  implement는 GPT-6.1 Sol / high, implementSmall과 verify는 GPT-6 Luna / high, 작은 리뷰(`chef_reviewer_small`)는 GPT-6.1 Terra / high로 고정하며 미등록 파견 세부는 `codex-roles.md`를 따른다.
- 어댑터 `rolePolicy`(`runtimes.codex.rolePolicy`가 최상위 값을 덮는다)는 이 스킬이 역할을 파견할 때만 적용한다. 없으면 `model: "definition"`, `subagents: true`다.
  `model: "inherit"`면 미등록 파견에서 `model`·`reasoning_effort` 인자를 생략해 부모를 상속하며 위의 고정보다 우선한다(등록된 역할은 정의의 모델이 적용되므로 그 경우를 보고한다). `definition`이면 위 고정을 따른다.
  `subagents: false`면 `chef_developer_medium`(과 `roles`가 가리키는 같은 계열 역할)의 브리프에 "하위 에이전트를 띄우지 않는다. 나눠야 하면 단위·파일 경계·계약을 담은 분할안을 부모에게 반환한다"를 넣는다.
  도구로 막는 강제 차단이 아니라 역할 정의가 브리프를 따르게 하는 것이며, 코디네이터가 반환된 분할안으로 새 단위를 파견한다.
- 어댑터의 `runtimes.codex` 덮어쓰기를 적용한다. 병합 순서는 adapter → adapter.runtimes.codex → local → local.runtimes.codex다.
- CLI에서 hooks 기능이 켜져 있고 `/hooks`에서 현재 플러그인 훅을 신뢰한 상태여야 한다.
  단독 마커 뒤 ACTIVE와 훅 안내가 모두 확인되기 전에는 미션 파일·프로젝트 파일을 쓰지 않는다.
- 프로젝트의 AGENTS.md와 실제 설치된 Codex 스킬을 우선 확인한다. Claude용 도구 이름·스킬이
  그대로 있다고 가정하지 않는다. 필요한 PoC·리뷰·Unity MCP가 없으면 해당 단계를 미확인으로 보고하고 멈춘다.

## 실행 경로와 검증 범위

- `<mission-entry>`: `<plugin-root>/codex/skills/chef-mission/scripts/mission_entry.py`, `<mission-entry-runtime>`: 빈 값.
- `<mission-state-runtime>`: 빈 값.
- `<mission-state>`: `<plugin-root>/codex/skills/chef-mission/scripts/mission_state.py`, `<p4-wrapper>`: `<plugin-root>/codex/skills/chef-mission/scripts/p4c.py`.
- `<distill-skill>`: `<plugin-root>/codex/skills/chef-distill/SKILL.md`. 슬롯은 `$chef:chef-slots`, 어댑터는 `$chef:chef-adapter-init`이다.
- 어댑터 검색·설정은 [공통 어댑터 계약](references/adapter-schema.md)의 Codex 절을 따른다.
- 검증 명령은 변경 경로·계약을 실제로 덮어야 한다. 프로젝트 전역 실패를 별도 게이트로 유지하고 약한 타깃 검사로 대체하거나 범위를 우회하지 않는다.
  요청한 각 선택자가 실제 카탈로그나 결과에서 해석·선택되고 요청 범위가 실행됐다는 증거를 확인한다. 직접 실행 목록 또는 프로젝트 실행 계약이 선택자와 결과를 연결하는 근거를 쓴다. 근거가 부족하면 미확인으로 반환한다.
  카탈로그에서 선택된 사실이나 양수 전체 실행 수만으로 실제 실행을 입증하지 못한다. 클래스·매개변수화 사례는 요청된 의미 범위의 실행 근거가 필요하며 필터 선택 수와 실행 수는 일반적으로 다르다. 프로젝트가 범위를 기록하면 `results[].equals`·`results[].positive`로 검사한다.
- 첫 심층 리뷰와 확대는 fresh `roles.review`(없으면 `chef_code_reviewer`)다. 좁은 수정 확인과 공통 절차의 작은 문서 첫 리뷰는 `chef_reviewer_small`이며 변경 전 본 대비 diff를 넘긴다. 자세한 라우팅·모델·기본 역할은 [Codex 역할](references/codex-roles.md)을 따른다.
