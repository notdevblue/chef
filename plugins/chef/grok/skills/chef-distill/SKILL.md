---
name: chef-distill
description: "Grok CLI 세션이나 미션 대화 기록에서 사용자 피드백을 뽑아 반영 후보로 분류할 때 쓴다. 피드백 정리해줘, 지침에 반영할 거 정리해줘, 이번 세션 피드백 정리, chef-distill 요청에 사용한다. 기록에 없는 피드백을 만들지 않는다."
---

# Grok distill

실행 전에 [공통 절차](../../../shared/skills/distill.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<distill-script>`: `<plugin-root>/skills/distill/scripts/distill.py`, `<distill-runtime>`: `--runtime grok`.
- Grok SessionStart는 `new`·`load` 이벤트의 대화 기록 경로를 컨텍스트에 전달한다. ACP `updates.jsonl`은 추출기가 읽을 수 있지만, `--transcript`에는 컨텍스트나 세션 기록에 명시된 실제 경로를 사용한다. 최신 파일을 선택하거나 경로를 추측하지 않는다.
Grok은 Claude의 `HAN_TRANSCRIPT`를 자동 경로로 사용하지 않는다. extract에는 명시한 `--transcript`가 필요하다.
미션이면 `mission_state.py transcripts`가 출력한 경로마다 `--transcript`를 붙인다. 출력에 없는 경로는 넣지 않는다.
추출 출력에 없는 문장을 피드백으로 만들지 않는다. 남길 것이 없으면 그렇게 말하고 `record --verdict 확인`으로 워터마크만 올린다.

`--until`은 extract가 출력한 그 세션의 until이다. 로그는 `HAN_DISTILL_LOG`다. 비어 있으면 Grok은 `GROK_HOME/han/distill/log.tsv`에 기록하며 `GROK_HOME`이 비어 있으면 `~/.grok/han/distill/log.tsv`다.
처음부터 다시 보려면 `--all`이다.

- 메모리 저장과 개인 규칙의 다른 런타임 사본 반영은 현재 환경의 지침과 사용자 선택을 따른다.
