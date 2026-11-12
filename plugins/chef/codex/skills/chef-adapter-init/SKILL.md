---
name: chef-adapter-init
description: 프로젝트를 조사해 $chef:chef-mission 이 쓰는 어댑터(adapter.json · adapter.md) 초안을 만들거나, 이미 있는 어댑터를 프로젝트 현재 상태와 대조해 고친다. 슬롯 워크스페이스를 둘 폴더도 이때 묻는다. "미션 어댑터 만들어줘", "이 프로젝트에 미션 붙여줘", "어댑터 점검해줘", "$chef:chef-adapter-init" 요청에 사용한다.
---

# Codex adapter-init

실행 전에 [공통 절차](../../../shared/skills/adapter-init.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<check-guard-script>`: `<plugin-root>/codex/skills/chef-adapter-init/scripts/check_guard.py`, `<check-guard-runtime>`: 빈 값.
- 저장소 안 어댑터는 `<프로젝트>/.codex/orchestrator/`다. 레지스트리는 셸 `HAN_ADAPTERS_DIR` → `<CODEX_HOME>/config.toml` 또는 `~/.codex/config.toml`의 `[shell_environment_policy.set].HAN_ADAPTERS_DIR` → 저장소 직접 실행일 때 `<저장소>/adapters` 순이다. 새 설정 키를 만들지 않는다.
- [공통 어댑터 계약](../chef-mission/references/adapter-schema.md)의 Codex 절을 따른다. 역할·준비 단계는 `runtimes.codex`에 두고 Claude 설정·메모리 준비와 AGENTS.md·Codex MCP 준비를 혼동하지 않는다.
- [Codex 역할](../chef-mission/references/codex-roles.md)의 조사 지시를 받은 에이전트를 사용한다. 등록이 없으면 다른 에이전트로 대신하거나 역할 지시를 프롬프트로 넘기지 않고 사용자에게 알린다. research·review는 부모 상속, implement는 GPT-6.1 Sol / high, implementSmall·verify는 GPT-6 Luna / high다.
- `runtimes.codex.roles.verify`는 `chef:verifier`이며 첫 슬롯은 `$chef:chef-slots create`다.
