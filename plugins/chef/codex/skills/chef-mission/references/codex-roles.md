# Codex 역할 파견

플러그인 루트의 `codex/agents/*.toml`에 역할별 지시가 있다. 프로젝트의 AGENTS.md와 함께 적용한다.
Codex는 플러그인의 이 폴더를 사용자 에이전트 폴더로 자동 등록하지 않는다.

| 역할 | 기본 지시 |
| --- | --- |
| research, 기존 chef:researcher | `codex/agents/chef-researcher.toml`의 developer_instructions |
| implement, 기존 chef:dev-medium | `codex/agents/chef-developer-medium.toml`의 developer_instructions, GPT-6.1 Sol / high 고정 |
| implementSmall | `codex/agents/chef-developer-small.toml`의 developer_instructions, GPT-6 Luna / high 고정 |
| verify | `codex/agents/chef-verifier.toml`의 developer_instructions, GPT-6 Luna / high 고정 |
| review | 변경 범위·명세·완료 조건을 받아 결함과 근거만 보고한다. 파일을 편집하지 않는다. |

등록된 에이전트가 있으면 사용한다. 없으면 해당 TOML의 developer_instructions와 마일스톤 브리프를
새 서브에이전트의 프롬프트로 넘긴다. 이름만으로 Claude 에이전트를 실행할 수 있다고 가정하지 않는다.
사용자·프로젝트가 지정한 다른 역할은 실제 정의를 읽는다. 필수 정의를 찾지 못하면 알리고 멈춘다.
리뷰는 구현 에이전트를 재사용하거나 구현 대화 내역을 fork하지 않고 새 에이전트로 실행한다.

코디네이터가 규칙·원문·승인·양식의 실제 경로와 해당 절을 확정해 구현자·리뷰어에게 같은 묶음을 준다.
각 역할이 다시 찾다가 누락·충돌을 발견하면 코디네이터에게 반환해 양쪽 묶음을 함께 맞춘다. 프로젝트 자료는 플러그인에 복사·요약·익명화하지 않는다.
구현자는 변경 함수·블록 또는 문서 전체를 대조해 명확한 결함·규칙 위반을 먼저 고치고 자기 확인 근거와 남은 판단을 반환한다. 지적 수정 라운드는 지적한 줄과 직접 영향만 고치고 바뀐 줄·호출부만 점검하며 지적 밖에서 바꾼 줄은 목록으로 반환한다.
관련 승인 수정은 담당 구현자에게 이어 맡길 수 있으나 PoC 원본 스킬의 fresh 요구와 독립 리뷰는 유지한다.
리뷰는 근거 있는 결함·명시적 규칙 위반·계약·호출부를 한 번에 수집하고 제안은 분리한다. 읽지 못한 경로는 검증 공백으로 적는다. 문서는 원문·최신 답·승인·규칙·양식·예시를 처음부터 함께 받아 의미·보존·문체도 한 번에 본다.
finding은 고정 id·`severity`·원인·이전 해소 근거로 연결하며 미해결·새 지적·회귀를 구분한다. 열린 지적 0건(미해결 포함)에 제안만 남으면 clean이고 제안은 루프 안에서 적용하지 않는다. 후속은 `review-scope`의 파일별 diff·직전 지적·영향 계약을 보고,
미변경 부분은 실제 결함·규칙·원문 근거가 있는 Medium 이상일 때만 다시 열고 Low는 제안으로 둔다.
Low만 남으면 코디네이터가 `verification-workflow.md`의 closed-low 조건(비회귀, 지적 줄 범위·계약 불변, 연속 금지, 리뷰 중 입력 변경 없음)을 확인하고 diff로 해소를 확인해 `closed-low`로 닫으며 reviewer를 다시 띄우지 않는다. 조건이 안 되면 독립 재리뷰를 하거나 사용자에게 묻는다.
3라운드·timebox 상한에는 남은 원인·참조 누락·회귀·판단·수정과 검증을 진단해 반환한다.

research와 review의 모델·추론 수준은 부모 설정을 상속한다. implement는 `gpt-6.1-sol`과 high,
implementSmall과 verify는 `gpt-6-luna`와 high로 고정한다(어댑터 `rolePolicy.model`이 `inherit`면 아래 역할 정책이 우선).
등록된 `chef_developer_small` 또는 `chef_verifier`가 없으면 해당 역할의 지시·브리프와
`model: "gpt-6-luna"`, `reasoning_effort: "high"`, `fork_turns: "none"`을 넘긴다.
등록된 `chef_developer_medium`이 없으면 해당 역할의 지시·브리프와 `model: "gpt-6.1-sol"`,
`reasoning_effort: "high"`, `fork_turns: "none"`을 넘긴다. 중첩 구현 파견에도 같은 모델·추론 수준을 지정한다.
고정 모델·추론 수준을 지정할 수 없으면 다른 모델로 대신 실행하지 않고 코디네이터에게 미확인으로 반환한다.
실행 중 모델 전환·자동 승격은 하지 않는다.
그 밖의 별도 모델은 사용자나 프로젝트 설정이 정한 경우만 쓴다.
읽기 전용은 역할 지시이며 별도 샌드박스를 뜻하지 않는다. 권한은 부모 세션에서 상속한다.

## 역할 정책

어댑터 `rolePolicy`(`runtimes.codex.rolePolicy`가 최상위 값을 덮는다)는 mission 스킬이 역할을 파견할 때만 적용한다. 직접 호출과 TOML의 모델 값은 바꾸지 않는다. 없으면 `model: "definition"`, `subagents: true`다.

- `model: "inherit"` — 미등록 파견에서 `model`과 `reasoning_effort` 인자를 생략해 부모의 모델·추론 수준을 상속한다. `fork_turns: "none"`과 역할 지시·브리프는 그대로 넘긴다. 등록된 역할은 TOML의 모델이 적용되므로 상속을 지킬 수 없다. 그 경우를 코디네이터가 보고한다.
- `model: "definition"` — 위 고정을 따른다.
- `subagents: false` — `chef_developer_medium`(과 `roles`가 가리키는 같은 계열 역할)의 브리프에 "하위 에이전트를 띄우지 않는다. 나눠야 하면 단위·파일 경계·계약을 담은 분할안을 부모에게 반환한다"를 넣는다. TOML은 기본(허용)을 유지하고 브리프의 금지 지시를 따른다. 도구로 막는 강제 차단이 아니다.

선택적으로 `codex/agents/*.toml`을 `~/.codex/agents/`에 복사하면 해당 이름을 등록할 수 있다.
기존 파일이 있으면 먼저 차이를 보고하고 덮어쓰지 않는다. 이 플러그인 설치만으로 전역 파일을 쓰지는 않는다.
