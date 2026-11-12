---
name: chef-adapter-init
description: "Grok CLI에서 미션 어댑터(adapter.json · adapter.md) 초안을 만들거나 점검할 때 쓴다. 미션 어댑터 만들어줘, 이 프로젝트에 미션 붙여줘, 어댑터 점검해줘, chef-adapter-init 요청에 사용한다. 값을 지어내지 않는다."
---

# Grok adapter-init

실행 전에 [공통 절차](../../../shared/skills/adapter-init.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<check-guard-script>`: `<plugin-root>/skills/adapter-init/scripts/check_guard.py`, `<check-guard-runtime>`: `--runtime grok`.
- 저장소 안 어댑터는 `<프로젝트>/.grok/orchestrator/`다. 개인 레지스트리는 `HAN_ADAPTERS_DIR`이고 Claude `pluginConfigs`의 `adapters_dir` 주입을 받지 않는다.
- 환경변수가 비었으면 `HAN_REPO/adapters`를 레지스트리로 쓸지 묻고 경로를 정해 달라고 한다. 폴더를 추측해서 만들지 않는다.
- 계약은 [공통 어댑터](../../../skills/mission/references/adapter-schema.md)의 Grok 절, 파견은 [Grok 역할](../chef-mission/references/grok-roles.md)이다. 첫 슬롯은 `chef-slots`로 만든다.
