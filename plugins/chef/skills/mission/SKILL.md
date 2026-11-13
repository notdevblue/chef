---
name: mission
description: 작업 하나를 단계(컨텐츠 확인 → PoC → 구현 → 검토)와 마일스톤으로 나눠 조사·설계·구현·리뷰를 서브에이전트에 파견하고, 사람에게는 정해진 확인 지점에서만 승인을 받고 중간에는 보고만 한다. "미션으로 돌려줘", "이 작업 미션으로 진행해", "마일스톤 나눠서 알아서 진행해", "에이전트들 시켜서 끝까지 해줘", "자율로 진행하고 보고만 해줘", "/chef:mission" 요청에 사용한다. "미션 이어서"면 이어서 하기, "미션 중단"이면 중단 절차를 따른다. 어댑터(저장소 안 `.claude/orchestrator` 또는 개인 레지스트리)가 있어야 동작한다. 한두 파일짜리 작은 수정에는 쓰지 않는다.
---

# Claude mission

실행 전에 [공통 절차](../../shared/skills/mission.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 두 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 경로와 역할

- `<mission-entry>`: `<plugin-root>/skills/mission/scripts/mission_entry.py`, `<mission-entry-runtime>`: `--runtime claude`.
- `<mission-state-runtime>`: 빈 값.
- `<mission-state>`: `<plugin-root>/skills/mission/scripts/mission_state.py`, `<p4-wrapper>`: `<plugin-root>/skills/mission/scripts/p4c.py`.
- `<distill-skill>`: `<plugin-root>/skills/distill/SKILL.md`. 슬롯은 `/chef:slots`, 어댑터는 `/chef:adapter-init`이다.
- 어댑터 검색·설정은 [공통 어댑터 계약](references/adapter-schema.md)의 Claude 절을 따른다. `${CLAUDE_PLUGIN_ROOT}`를 실제 루트로 쓸 수 있다.
- 조사·구현·검증 파견은 실제 등록된 Agent 역할과 도구를 쓴다. 검증은 `roles.verify`(없으면 `chef:verifier`), 심층 리뷰는 `roles.review`(없으면 `chef:code-reviewer`), 작은 리뷰는 `roles.reviewSmall`(없으면 `chef:reviewer-small`), 감사는 `roles.audit` 또는 `roles.review`다.
- 지정된 역할·모델을 사용할 수 없거나 파견이 실패하면 대체하지 않고 근거·미완료 범위를 반환한다.
- 기존 Sonnet 모델을 유지하고 `chef:dev-small`·`chef:reviewer-small`·`chef:researcher`는 medium, `chef:verifier`는 Sonnet 5.5 / low, `chef:dev-medium`은 high다. 첫 심층 리뷰·기록 감사·설계 판단은 Opus / high다.
- 편집·리뷰 역할은 역할별 builtin 도구만 명시한다. verifier는 부모 MCP 연결을 유지하고 ToolSearch가 있으면 필요한 검증 도구만 찾는다. MCP 상속·검색이 미확인이면 검증 가능으로 보고하지 않는다. 도구 축소의 사용량 효과는 측정 전 미확인이다.

**역할 정책** — 어댑터 최상위 `rolePolicy`(`references/adapter-schema.md`)는 이 스킬이 역할을 파견할 때만 적용한다.
없거나 키가 없으면 `model: "definition"`, `subagents: true`다. 스킬 밖 직접 호출과 역할 정의 파일의 모델 값은 바꾸지 않는다.
- `model: "inherit"` — 이 스킬의 모든 역할 파견에서 Agent 호출 `model` 인자에 내 모델의 별칭(`sonnet`·`opus`·`haiku`·`fable`)을 넘긴다.
  호출 인자는 별칭만 받으며 `inherit`나 전체 모델 ID는 줄 수 없다. 내 모델이 별칭에 대응하지 않으면 정의 모델로 두고 그 사실을 보고한다.
  effort 지시는 그대로다. 아래 Sonnet·Opus 배정과 verifier 고정보다 이 정책이 우선한다.
- `model: "definition"` — 아래 모델 지시를 그대로 따른다. Claude 리뷰·검증 파견은 그 별칭을 Agent 호출 `model` 인자에 넣는다. 인자를 비우면 역할 파일의 `inherit`가 코디네이터 모델이 되므로 리뷰·검증에서는 비우지 않는다.
- `subagents: false` — `chef:dev-medium`(어댑터 `roles`가 가리키는 같은 계열 역할 포함)에 파견할 때 브리프에
  "하위 에이전트를 띄우지 않는다. 나눠야 하면 단위·파일 경계·계약을 담은 분할안을 부모에게 반환한다"를 넣는다.
  역할 정의가 브리프의 금지 지시를 따르게 하는 것이며 도구로 막지는 않는다. 반환된 분할안으로 새 단위를 파견하는 것은 내 몫이다.

4. **마일스톤 리뷰** — 검증이 끝난 단위부터 진행한다. 파일 소유가 겹치지 않으면 그 리뷰를 같은 턴에 파견한다. `rolePolicy.model`이 `definition`이면 리뷰 파견의 Agent 호출 `model` 인자에 별칭만 넣는다(`opus` 또는 `sonnet`). 역할 이름이나 역할 파일의 `model: inherit`로 모델을 정하지 않는다. `inherit`면 그 정책이 우선한다. fork는 쓰지 않는다.
   첫 심층 리뷰는 fresh `roles.review`(없으면 `chef:code-reviewer`)에 `model: "opus"`다(아래 작은 문서 변경은 예외).
   직전 finding의 수정 확인은 같은 `roles.review`에 `model: "sonnet"`이다. 프로젝트 규약이 `roles.review` 역할만 읽게 정하면 `roles.reviewSmall`로 바꾸지 않는다. 그 규약이 없는 좁은 확인과, 뜻·승인 범위가 그대로인 작은 문서 변경(새 규칙·규칙 관계 변경과 질문·보고 문서는 제외)의 첫 라운드만 `roles.reviewSmall`(없으면 `chef:reviewer-small`)에 `model: "sonnet"`을 넣고, 그 브리프에는 변경 전 본 대비 diff를 넣는다(`references/usage-policy.md`).
   원인 불명, 공통 계약·구조 변경, 판단 충돌일 때만 그 근거를 브리프에 적고 같은 역할에 `model: "opus"`로 확대한다. 독립 재리뷰는 이 Sonnet 재리뷰이거나 그 확대이며, 첫 라운드 Opus 호출을 반복하는 뜻이 아니다. 프로젝트 리뷰 규약은 두 역할 모두 지킨다.

검증 파견은 Agent 호출 `model: "sonnet"`을 명시한다. `rolePolicy.model: "inherit"`가 우선하며 역할·모델 지정 불가 시 미확인으로 반환한다.
사용량 측정의 Claude JSONL 계약은 [Claude 사용량](references/claude-usage.md)을 따른다.
