---
name: chef-mission
description: "Grok CLI에서 작업을 단계와 마일스톤으로 나눠 조사·구현·검증·독립 리뷰를 파견하고 확인 A·B·C를 받는다. 미션 진행·이어서 하기·중단·상태 또는 chef-mission 요청에 사용한다. S는 미션 없이 처리하고 M·L은 코디네이터로 진행한다."
---

# Grok 미션 오케스트레이션

이 세션은 M·L 미션의 코디네이터다. 프로젝트 파일은 역할별 에이전트에 맡기고, 미션 기록과 승인·파견·결과 수집을 관리한다.
기존 Grok 상태 명령만 실행하거나 `chef-run`으로 직접 구현한 뒤 끝내지 않는다.

## 공통 절차 읽기

`<plugin-root>`는 이 파일의 폴더에서 세 단계 위인 `plugins/chef`가다. 실제 설치 경로를 확인하고 절대 경로로 치환한다.
`<repo>`는 `HAN_REPO` 또는 그 플러그인을 담은 체크아웃이다. 경로를 추측하지 않는다.

실행 전에 다음 파일을 읽는다. 공통 절차를 이 파일에 복사하지 않는다.

1. `<plugin-root>/skills/mission/SKILL.md` 전체: 컨텐츠 확인 → PoC → 구현 계획 → 구현·검증·독립 리뷰 → 통합 검토·기록 감사 → 확인 C·종료·중단·이어서 하기.
2. `<plugin-root>/grok/skills/chef-mission/references/grok-roles.md`: 실제 역할·도구·모델·effort 계약.
3. 공통 `references/verification-workflow.md`와 `references/usage-policy.md`: 검증 계획·입력 동결·receipt 재사용·리뷰 범위·완료 판정·작은 브리프.
4. 작업 시점에 공통 절차가 가리키는 나머지 참고 자료. 양식·질문 기록·사람용 문서는 `<repo>/plugins/chef/skills/mission/references/`에 있다.

공통 절차와 모든 하위 참조의 **런타임 전용 규칙**은 아래 Grok 계약으로 대체한다. 공통 단계·승인 범위·독립성·증거 요구는 그대로 따른다.
Claude·Codex 모델 고정, fallback 에이전트, 도구 이름과 호출 예시는 Grok에 적용하지 않는다.

## Grok 실행 계약

- 어댑터 검색은 `.grok/orchestrator` → `.claude/orchestrator` → `.codex/orchestrator` → `HAN_ADAPTERS_DIR` → 저장소 `adapters`다. 검색·병합의 정본은 공통 `references/adapter-schema.md`의 Grok 절과 `--runtime grok` 실행 결과다.
  `adapter → adapter.runtimes.grok → local → local.runtimes.grok` 순으로 적용한다. 잘못 설정된 경로를 다른 어댑터로 우회하지 않는다.
- 공통 문서의 `${CLAUDE_PLUGIN_ROOT}`·Codex 스크립트 경로는 실제 `<plugin-root>`로 치환한다.
  미션 상태 명령은 항상 아래 공유 CLI에 `--runtime grok --project "<작업 루트>"`를 붙인다. 다른 미션은 `--mission <id>`를 붙인다.

```text
python "<plugin-root>/skills/mission/scripts/mission_state.py" --runtime grok --project "<작업 루트>" <명령>
```

- `check_human_doc.py` 등 공통 스크립트는 `<plugin-root>/skills/mission/scripts/` 아래 실제 경로로 호출한다.
  공통 문서의 p4 래퍼 경로는 `<plugin-root>/grok/skills/chef-mission/scripts/p4c.py`로 대체한다. 이 래퍼가 `runtime="grok"`을 공유 코드에 전달한다.
  `guard.p4Client.require`가 켜져 있으면 p4 쓰기는 이 Grok 래퍼로 한다. 런타임을 받는 다른 CLI도 `--runtime grok`을 쓴다.
- 공통 문서의 `Bash`는 현재 Grok 셸 도구, `Read`·`Grep`·`Glob`은 실제 파일 읽기·검색 도구로 대체한다.
  파견·수집은 `grok-roles.md`의 native 도구만 쓴다. 도구가 없으면 그 단계를 미확인으로 반환하고 직접 구현이나 외부 봇 인계로 우회하지 않는다.
- 공통 `roles.*`와 Claude/Codex 역할 이름·모델·effort 지시는 모두 `grok-roles.md`의 Grok 역할로 대체한다.
  `verification-workflow.md`의 verifier 모델 고정, `usage-policy.md`의 Sonnet/Opus 배정도 이 대체에 포함된다. 공통 문서의 `chef:reviewer-small`은 Grok `chef:reviewer-small`(상속 / medium)이다.
  현재 선택된 native Grok 4.7 모델을 파견에 상속한다. 코디네이터 자신의 현재 effort는 바꾸지 않는다.
- `/chef:adapter-init`·Codex adapter-init 호출은 Grok `chef-adapter-init`, 슬롯 호출은 `chef-slots`, `../distill/SKILL.md`는 `<plugin-root>/grok/skills/chef-distill/SKILL.md`로 대체한다.
  어댑터의 프로젝트 스킬은 실제 설치 파일과 도구를 읽고 적용한다. 필요한 스킬·Unity 연결이 없으면 해당 단계를 미확인으로 남긴다.
  Claude JSONL 전용 사용량 집계기는 Grok 로그에 적용하지 않는다. 실제 모델·effort 증거와 미확인 한계만 기록한다.

## 시작과 승인

새 미션과 이어서 하기는 어댑터 탐색·상태 조회보다 먼저 현재 Grok 세션 id와 실제 cwd로 공통 진입을 실행한다.
새 미션은 후보 id, 이어서 하기는 기존 미션 id를 쓴다. 기존 id를 알 수 없으면 임의로 만들지 않는다.

```text
python "<plugin-root>/grok/scripts/mission_entry.py" --project "<cwd>" --mode <new|resume> --session "<현재 세션 id>" --mission "<미션 id>" --task "<작업>"
```

`continue`면 반환 어댑터를 읽고 아래 절차로 간다. `delegate`와 `launched: true`면 부모에서 멈추고 슬롯이 이어받는다.
`needs-input`과 실패는 미션을 열지 않고 선택·오류를 알린다. 슬롯 세션도 자기 세션 id로 진입한다.
등록된 진입의 resume은 기존 미션의 세션 id·런타임을 유지한다. 현재 세션이 다르면 소유권을 바꾸지 않고 멈춘다.
open·resume 직전에는 같은 입력의 진입 성공이 필요하다. 등록·route·receipt는 공통
[`mission-entry.md`](../../../../skills/mission/references/mission-entry.md)를 따른다. 중단·닫기만 하면 진입을 실행하지 않는다.

1. 프로젝트의 `AGENTS.md`·`CLAUDE.md`와 관련 workflow·어댑터를 읽고 진입 티어를 정한다.
   S(한두 파일·단일 수정)는 `chef-run`의 직접 처리 경로다. 사용자가 미션을 명시적으로 원할 때만 `init --tier S --force-tier`로 열고 코디네이터 분리를 유지한다.
   M·L은 공통 절차의 파견 미션으로 진행한다.
2. 먼저 `active`·`status`·`context`를 읽는다. 출력에 없는 미션 id·단계·목표·승인을 만들지 않는다.
   다른 세션의 ACTIVE를 이어받기 전에는 소유 세션과 사용자의 이어받기 지시를 확인한다. 끊김 의심만으로 닫거나 가로채지 않는다.
3. `echo han-mission open <id>` 또는 `echo han-mission resume <id>`는 **단독 명령**으로 실행한다.
   훅의 가드 안내와 ACTIVE의 미션 id·현재 세션 id가 모두 맞는지 확인한 뒤 `init` 또는 이어서 하기를 진행한다. echo 출력만으로 성공을 판단하지 않는다.
4. 확인 A는 PoC 설계 승인, B는 PoC 결과·구현 계획 승인, C는 결과 확인이다. PoC 불필요가 확인되면 A를 생략한다.
   기존 대화·질문 정본에서 이미 승인된 범위와 경로는 다시 승인받지 않는다. 부분 승인을 단계 전체 승인으로 올리지 않는다.
   계획·결과 확인은 submit·push·배포·외부 전송 승인이 아니다. 사용자가 이미 명시한 해당 작업의 승인은 그 범위 그대로 따른다.
5. 공통 절차의 질문 JSON 초안 → 미리보기·원문 대조·독립 검토 → 발행·검사·공유 → 답 원문 기록을 따른다.
   `question check`와 공유 확인을 통과하기 전에 공유 완료로 보고하지 않는다. 승인된 독립 작업은 답 대기 중에도 진행한다.

## 파견과 완료

- 코디네이터만 미션 상태를 쓴다. `ACTIVE`, `mission.md` 머리 필드·상태 TSV·질문 정본·생성본은 상태 CLI와 가드가 담당한다.
  구현·조사·검증·리뷰 에이전트는 대조 범위·적용 자료의 실제 경로와 절·기계 검사와 결과·미확인 또는 제외 범위와 이유·실제 변경·근거·미완료 범위를 간결히 반환한다. 일부만 대조했다면 전체 준수처럼 보고하지 않는다. 자기 확인은 기계적 준수 증명이나 독립 리뷰를 대신하지 않는다. verifier의 지정 실행기가 생성하는 `verification/` 기록은 예외다.
- 구현은 승인된 목표·파일 소유·계약·검증·금지 범위를 브리프로 파견한다. `M1`을 끝까지 완료한 뒤, 선행 `done`·소유·공유 자원 조건을 충족하는 단위는 공통 §5대로 한 번에 맡긴다.
  코디네이터가 관련 규칙·원문·승인·양식의 실제 경로와 해당 절을 한 번 정해 구현·수정·첫 리뷰·재리뷰 브리프가 모두 같은 묶음을 가리키게 한다. 누락·충돌은 부모에게 반환해 함께 맞춘다.
  첫 구현의 구현자는 변경 함수·블록 또는 문서 전체를 대조해 명확한 결함·규칙 위반을 고친 뒤 자기 확인 근거·남은 판단을 반환한다. 지적 수정 라운드는 지적한 줄과 직접 영향만 고치고 바뀐 줄·호출부만 점검하며 지적 밖에서 바꾼 줄은 목록으로 반환한다. 프로젝트 자료를 플러그인에 복사·요약·익명화하지 않는다.
  chef의 Grok 파견은 깊이 1로 운영한다. 자식은 다시 spawn하지 않고 필요한 분할 브리프를 부모에게 반환한다. 코디네이터가 겹치지 않는 범위로 다시 파견하고 전체 미완료 범위를 추적한다.
- 검증은 단위마다 고정 계획의 verifier 하나다. 같은 검증 자원을 쓰는 단위만 모아 한 명이 실행하고, 자원이 갈리면 공통 §5대로 병렬로 맡긴다. 입력 동결·환경·실행 수·결과·로그를 확인한다. 실패·timeout·테스트 0건·부분 반환·턴 상한은 done이나 승인 요청의 근거가 아니다.
  승인 범위 안의 남은 일을 진단해 새 브리프로 이어간다. 실제 멈춤 조건·리뷰 3라운드·timebox는 유지한다.
  3라운드에서 남은 것이 Low뿐이면 코디네이터가 `closed-low`로 닫는다. closed-low 조건이 안 되면 독립 재리뷰를 하거나 사용자에게 묻는다.
  Medium 이상이 남았거나 수정끼리 충돌하거나 같은 지적이 재발하거나 timebox를 넘기면 미해결 원인·참조 누락·회귀·남은 판단·수정과 검증을 진단한다. 같은 일을 더 돌리는 연장안만 묻거나 품질·검증 범위를 낮추지 않는다.
- 리뷰와 기록 감사는 구현자와 다른 fresh reviewer다. 구현 대화를 상속하거나 구현자를 재사용하지 않는다. `roles.review`가 없거나 `code-reviewer`·`chef:code-reviewer`면 `chef:reviewer`다. 좁은 재리뷰와 뜻·승인 범위가 그대로인 작은 문서 변경(새 규칙·규칙 관계 변경과 질문·보고 문서는 제외)의 첫 리뷰는 `chef:reviewer-small`이고 그 브리프에는 변경 전 본 대비 diff를 넣는다. 뜻·승인 범위 변경 등을 반환하면 review 매핑의 심층 리뷰어(기본 `chef:reviewer`)로 확대한다(`grok-roles.md`).
  리뷰 파견 직전 `review-start <id> --plan …`으로 입력 snapshot을 기록하고 돌아올 때까지 그 범위를 고치지 않는다. 어댑터가 정한 lint·규약 검사는 검증 계획 `commands[]`로 먼저 통과시키고 출력을 리뷰어에게 준다.
  첫 리뷰는 결함·규칙 위반·계약·호출부를 한 번에 대조하고 읽지 못한 경로는 검증 공백으로 적게 한다. 코디네이터가 그 공백을 같은 라운드 안에서 보강 지시한다.
  재리뷰는 같은 묶음과 `review-scope`의 파일별 diff·직전 finding·영향받는 계약만 본다. 문서 원문·최신 답·승인·규칙·양식·예시도 첫 리뷰에 함께 넘겨 의미·보존·문체를 한 번에 본다.
  제안은 분리하며 열린 지적 0건(미해결 포함)에 제안만 남으면 clean이다. 제안은 루프 안에서 적용하지 않고 후속 후보로 보고한다.
  finding의 고정 id·`severity`·원인·이전 해소 근거로 미해결·새 지적·회귀를 구분한다. 미변경 부분은 실제 근거가 있는 Medium 이상일 때만 다시 열고 Low는 제안으로 돌린다.
  같은 소유·원인·참조의 승인 수정은 묶어서 담당 작성자에게 돌리며 도구가 허용하면 이어 쓴다. PoC 원본 스킬의 새 에이전트 요구는 유지한다.
  열린 지적이 전부 Low이고 회귀가 아니며 수정이 지적한 줄 범위 안에서 시그니처·계약·공개 동작을 바꾸지 않으면 같은 구현자가 고친 뒤 코디네이터가 diff만 보고 `review --status closed-low`로 닫는다.
  그 밖은 독립 재리뷰이며 closed-low는 연속으로 쓰지 않는다.
  clean·findings·closed-low 모두 실제 결과 파일을 `review`로 기록하고 남은 지적·해소 확인 수를 남긴다(closed-low의 `--findings-count`는 0이거나 생략). closed-low의 전체 조건은 `references/verification-workflow.md`를 따른다.
- 최신 유효한 verify receipt와 독립 clean 리뷰 또는 코디네이터 closed-low 및 완료 조건 근거가 갖춰진 뒤 코디네이터가 `complete <id> --receipt … --review … --done-evidence …`를 호출한다.
  테스트 통과만으로 done을 쓰지 않는다. 상태 CLI가 거부하면 근거를 고치고 직접 상태를 덮어쓰지 않는다.
- 공통 통합 검토·명세 대조·기록 감사·보고·확인 C·피드백 선택 절차까지 마친다. 종료는 `mission set --state 닫힘`, 중단은 `--state 중단` 뒤 별도 호출의 `echo han-mission close <id>`다.
  재개 때 `context`·질문 정본·승인·소유·미완료 검증을 복원한다. 세션이 바뀌면 위 가드 재연결을 확인한다.
