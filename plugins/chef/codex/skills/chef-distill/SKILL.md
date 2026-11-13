---
name: chef-distill
description: Codex 세션이나 미션의 대화 기록에서 사용자 피드백을 뽑아 가이드·메모리·개인 규칙·플러그인 반영 후보로 분류하고, 사용자가 고른 것만 반영한다. "피드백 정리해줘", "지침에 반영할 거 정리해줘", "이번 세션 피드백 정리", "$chef:chef-distill" 요청과 chef-mission의 확인 C 직전에 사용한다.
---

# Codex distill

실행 전에 [공통 절차](../../../shared/skills/distill.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<distill-script>`: `<plugin-root>/codex/skills/chef-distill/scripts/distill.py`, `<distill-runtime>`: 빈 값.
- 일반 세션은 SessionStart 훅이 컨텍스트에 넣은 `chef-distill 대화 기록 경로`를 `--transcript`로 전달한다.
  Codex 훅은 부모 셸 환경을 바꾸지 않고 `CLAUDE_ENV_FILE`도 쓰지 않는다.
- 훅의 경로가 없으면 현재 세션의 `CODEX_THREAD_ID` 또는 `CODEX_SESSION_ID`와 정확히 일치하는
  `<CODEX_HOME>/sessions/**/rollout-*-<id>.jsonl`을 찾고 첫 `session_meta.payload.id`를 대조한다.
  `CODEX_HOME`이 없으면 `~/.codex`다. 경로를 확정할 수 없으면 사용자에게 묻는다.
  최신 파일이나 작업 폴더만으로 고르지 않는다(동시 세션의 기록을 집을 수 있음).
- 미션은 `mission_state.py transcripts`가 준 경로마다 `--transcript`를 붙인다. 누락된 기록은 알린다.
- Codex의 `response_item` 대화 메시지를 읽고 중복 이벤트·도구 결과·추론·주입된 지침을 제외한다.
  Claude 기록도 읽으므로 런타임을 바꿔 이어받은 미션은 경로 전부를 전달한다.
- 로그는 `HAN_DISTILL_LOG`, 기본 `<CODEX_HOME>/han/distill/log.tsv`이며 `CODEX_HOME`이 없으면 `~/.codex`다.
- 메모리는 현재 환경의 저장 규칙대로 쓰며 변경 노트를 요구하면 노트만 남긴다.
