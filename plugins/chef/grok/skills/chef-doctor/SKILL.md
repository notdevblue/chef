---
name: chef-doctor
description: "Grok Bot이 chef 어댑터를 못 찾거나 조회 환경이 안 맞을 때 쓴다. 발견 순서와 HAN_ADAPTERS_DIR, HAN_REPO를 확인하고 읽기 전용으로 끝낸다. 실행은 이 진단 다음의 별도 단계다."
---

# Grok doctor

실행 전에 [공통 절차](../../../shared/skills/doctor.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<doctor-script>`: `<plugin-root>/skills/doctor/scripts/doctor.py`, `<doctor-runtime>`: `--runtime grok`.
- `<check-guard-script>`: `<plugin-root>/skills/adapter-init/scripts/check_guard.py`, `<check-guard-runtime>`: `--runtime grok`.
- `<repo>`는 `HAN_REPO` 또는 실제 체크아웃이다. 공통 doctor의 `codex-agents`는 Codex 등록 점검이며 Grok 조회와 무관하다.

## Grok 조회 진단

`chef-status`의 `snapshot`이 종료 코드 2이거나, 어느 어댑터를 읽는지 불확실할 때 이 스킬로 원인을 가른다.
파일을 만들거나 고치지 않는다. Claude·Codex 플러그인 갱신으로 이 문제를 풀지 않는다.
이 스킬은 봇을 깨우지 않는다. 실행이 필요하면 진단 뒤에 코디네이터가 `chef-mission-brief`로 메인 또는 슬롯의 실행 봇을 깨운다.

## 순서

1. `<repo>/plugins/chef/grok/scripts/han_state.py`가 있는지 본다. 없으면 저장소 체크아웃 경로가 잘못된 것이다.
2. 상태 조회와 같은 명령으로 다시 읽는다.

```text
python "<repo>/plugins/chef/grok/scripts/han_state.py" snapshot --project "<프로젝트 폴더>"
```

3. stderr의 `오류:` 한 줄을 그대로 알리고, 아래 중 어디에 걸렸는지 말한다.
   - 가까운 폴더에 `.grok/orchestrator/adapter.json`, `.claude/orchestrator/adapter.json`, `.codex/orchestrator/adapter.json`이 없음
   - `HAN_ADAPTERS_DIR`가 비었거나 그 폴더에 `projects.json`이 없거나 `roots`가 프로젝트와 안 맞음
   - `HAN_REPO`를 지정했는데 `<HAN_REPO>/adapters`가 없음
   - 저장소에서 직접 실행하는데 `<repo>/adapters`가 없음
4. 계약은 `plugins/chef/skills/mission/references/adapter-schema.md`의 "Grok에서 상태 읽기"다.
   Claude 플러그인 설정과 Codex `config.toml`은 이 조회가 읽지 않는다. 훅 경로·패키징은 설치 환경에서 별도로 확인한다.

진단은 여기서 끝난다. 어댑터 초안을 쓰지 않는다. 이 스킬은 미션을 열지 않는다.
