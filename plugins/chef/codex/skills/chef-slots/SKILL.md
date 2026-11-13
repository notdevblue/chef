---
name: chef-slots
description: 미션을 동시에 여러 개 돌리기 위한 슬롯 워크스페이스를 만들고, 이어받고, 최신으로 갱신하고, 상태를 보고, 미션 세션을 띄우고, 지운다. "슬롯 만들어줘", "슬롯 상태", "슬롯 갱신", "슬롯에서 미션 돌려줘", "슬롯 지워줘", "미션 대시보드", "$chef:chef-slots" 요청에 사용한다. 슬롯 조작에는 slots 설정이 필요하며 미션 대시보드는 슬롯 없이도 동작한다.
---

# Codex slots

실행 전에 [공통 절차](../../../shared/skills/slots.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<slots-script>`: `<plugin-root>/codex/skills/chef-slots/scripts/slots.py`, `<slots-runtime>`: 빈 값.
- `<dashboard-script>`: `<plugin-root>/codex/skills/chef-slots/scripts/dashboard.py`, `<dashboard-runtime>`: 빈 값.
- 어댑터 검색·병합은 [공통 어댑터 계약](../chef-mission/references/adapter-schema.md)의 Codex 절을 따른다. Claude와 같은 슬롯 목록·잠금·ACTIVE를 공유한다.
- 새 탭은 `codex '$chef:chef-mission <작업>'`, 재개는 `codex '$chef:chef-mission 미션 이어서'`다. `run <이름> --resume --new-session`은 기존 대화 대신 새 Codex 세션에서 이어받는다. 기존 세션 기록이 없거나 다른 런타임이면 해당 런타임으로 재개하거나, 사용자가 Codex 인계를 요청한 뒤 이 옵션을 쓴다. 새 세션의 미션 절차가 다른 창의 실행·소유권을 확인한다.
- Windows 백그라운드 서버는 `Start-Process -WindowStyle Hidden`을 사용한다. 권한 검사가 파괴 옵션을 막으면 사용자가 터미널에서 직접 실행하도록 안내한다.

런타임별 지침 준비 명령은 `runtimes.codex.slots.beforeRun`에 둔다.
이 단계의 `{launchMode}`(`new`·`resume`·`prepare`)·`{sessionId}`는 부른 때와 검증된 기존 세션 UUID를 전달한다. `prepare`는 슬롯 `create`·`refresh` 직후이며 세션 UUID는 비어 있다.
새 세션의 빈 UUID 인자도 보존하도록 명령 값은 큰따옴표로 감싼다.
`slots.codexProjectDocMaxBytes`를 양의 정수로 설정하면 새 세션·재개 모두 Codex 프로젝트 지침 읽기 한도를 적용한다.
생략하면 기존 CLI 동작을 유지한다. 전체 계약은 `../chef-mission/references/adapter-schema.md`의 "슬롯"을 따른다.
슬롯에 준비한 native 역할 파일은 `slots.codexAgentConfigs`의 별칭별 `{configFile, description}`으로 등록한다.
파일 경로는 템플릿 확장 후 절대 경로여야 하며, `beforeRun` 성공 후 파일 확인을 통과해야 새 세션·재개를 연다.
dry-run은 생성 예정 파일이 없어도 등록할 명시 인자를 표시한다. 생략하면 기존 실행 인자를 유지한다.

- Codex 슬롯 준비는 어댑터의 `runtimes.codex.slots.prepare`를 적용한다. 예시 어댑터 기본값은 동기화·캐시 복사·링크·임포트·빌드다.
  Claude 설정·메모리 junction은 만들지 않는다. 프로젝트 AGENTS.md는 VCS 동기화로 받고,
  Unity MCP는 Codex 설정에서 슬롯의 projectPath에 붙는지 확인한다.
  프로젝트 로컬 `.codex/config.toml`·개인 AGENTS.md가 필요하면 어댑터 준비 단계에 명시한다.
  매번 실행 전에 갱신해야 하는 로컬 지침은 `beforeRun` 단계에 둔다.
  새 폴더 신뢰 및 훅 신뢰 확인은 사용자가 한다. 전역 Codex 설정은 수정하지 않는다.
