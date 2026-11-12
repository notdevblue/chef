---
name: distill
description: 세션(또는 미션)에서 사용자가 준 피드백을 대화 기록에서 뽑아, 지침·메모리·개인 규칙·플러그인에 반영할 후보로 분류해 보여 주고, 사용자가 고른 것만 반영한다. "피드백 정리해줘", "지침에 반영할 거 정리해줘", "이번 세션 피드백 정리", "/chef:distill" 요청에 사용한다. /chef:mission 의 확인 C 직전에도 이 절차를 쓴다.
---

# Claude distill

실행 전에 [공통 절차](../../shared/skills/distill.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 두 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<distill-script>`: `<plugin-root>/skills/distill/scripts/distill.py`, `<distill-runtime>`: 빈 값.
- Bash 도구로 돌린다. 훅이 내보낸 환경변수는 `CLAUDE_ENV_FILE` 을 읽는 Bash 도구에만 들어가고 PowerShell 에는 없다.
- 일반 세션은 `--transcript` 없이 돌린다. SessionStart 훅이 `HAN_TRANSCRIPT`(지금 세션의 대화 기록)를 내보낸다.
  비어 있으면 세션 시작 뒤 훅이 안 돈 것이니 사용자에게 대화 기록 경로를 묻는다. `ls -t` 로 최신 파일을 고르지 않는다
  (세션을 동시에 여러 개 띄우면 남의 기록을 집는다).
- 미션은 `mission_state.py transcripts` 가 준 경로마다 `--transcript` 를 붙인다.
- 일반 세션은 공통 extract 예시의 `--transcript`를 생략한다. 미션은 경로를 명시한다.
- 로그는 `HAN_DISTILL_LOG`, 기본 `~/.claude/han/distill/log.tsv`다.
- `rules` 후보가 있으면 Codex 사본도 같이 고칠지 따로 묻는다.
- 메모리는 기존 파일이 있으면 고치고 새로 만들면 `MEMORY.md`에 한 줄 색인을 단다. 현재 환경의 더 구체적인 메모리 저장 규칙이 있으면 그 규칙을 따른다.
