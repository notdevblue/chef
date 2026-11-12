---
name: adapter-init
description: 프로젝트를 조사해 /chef:mission 이 쓰는 어댑터(adapter.json · adapter.md) 초안을 만들거나, 이미 있는 어댑터를 프로젝트 현재 상태와 대조해 고친다. 슬롯 워크스페이스를 둘 폴더도 이때 묻는다. "미션 어댑터 만들어줘", "이 프로젝트에 미션 붙여줘", "어댑터 점검해줘", "/chef:adapter-init" 요청에 사용한다.
---

# Claude adapter-init

실행 전에 [공통 절차](../../shared/skills/adapter-init.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 두 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<check-guard-script>`: `<plugin-root>/skills/adapter-init/scripts/check_guard.py`, `<check-guard-runtime>`: 빈 값.
- 개인 레지스트리는 `HAN_ADAPTERS_DIR`다. SessionStart가 플러그인 `adapters_dir`를 Bash 환경에 주입한다. 비어 있으면 설정 누락을 알리고 README 설치 절차로 안내한다.
- 저장소 안 어댑터는 `<프로젝트>/.claude/orchestrator/`다. [공통 어댑터 계약](../mission/references/adapter-schema.md)의 Claude 절을 따른다.
- 조사 역할은 `researcher`(없으면 읽기 전용 에이전트), 기본 `roles.verify`는 `chef:verifier`다. 모델·도구는 실제 `agents/*.md` 정의와 mission의 Claude 실행 계약을 따른다.
- 첫 슬롯은 `/chef:slots create`로 만든다.
