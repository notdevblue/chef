# Codex 역할 파견

플러그인 루트의 `codex/agents/*.toml`에 역할별 정의가 있다. 프로젝트의 AGENTS.md와 함께 적용한다.
Codex는 플러그인의 이 폴더를 사용자 에이전트 폴더로 자동 등록하지 않는다. 등록해야 아래 `chef:*` 이름으로 파견할 수 있다.

| 역할 | 등록 이름 | 정의 파일 |
| --- | --- | --- |
| research | `chef:researcher` | `codex/agents/chef-researcher.toml`, 모델·추론 수준은 부모 상속 |
| implement | `chef:dev-medium` | `codex/agents/chef-dev-medium.toml`, GPT-6.1 Sol / high 고정 |
| implementSmall | `chef:dev-small` | `codex/agents/chef-dev-small.toml`, GPT-6 Luna / high 고정 |
| verify | `chef:verifier` | `codex/agents/chef-verifier.toml`, GPT-6 Luna / high 고정 |
| review, audit | `chef:code-reviewer` | `codex/agents/chef-code-reviewer.toml`, 모델·추론 수준은 부모 상속 |
| reviewSmall(좁은 재리뷰, 뜻·승인 범위가 그대로인 작은 문서 변경의 첫 리뷰) | `chef:reviewer-small` | `codex/agents/chef-reviewer-small.toml`, GPT-6.1 Terra / high 고정 |

옛 이름 `chef_researcher`·`chef_developer_small`·`chef_developer_medium`·`chef_verifier`·`chef_code_reviewer`·`chef_reviewer_small`은 위 `chef:*` 이름으로 읽는다. 새로 등록할 때는 `chef:*`만 쓴다.

등록된 에이전트만 파견한다. 등록되지 않았거나 필수 정의를 찾지 못하면 다른 에이전트로 대신하거나 TOML 본문을 새 서브에이전트에 붙여 넣지 않고, 사용자에게 알리고 정할 때까지 해당 단계를 미확인으로 둔다. 이름만으로 Claude 에이전트를 실행할 수 있다고 가정하지 않는다.
역할 이름은 어댑터 `roles`가 정한다. `roles.review`가 없으면 `chef:code-reviewer`, `roles.reviewSmall`이 없으면 `chef:reviewer-small`이다. `roles.audit`가 없으면 `roles.review`를 따른다.
좁은 재리뷰와 뜻·승인 범위가 그대로인 작은 문서 변경(새 규칙·규칙 관계 변경과 질문·보고 문서는 제외)의 첫 리뷰는 `roles.review`가 아니라 `roles.reviewSmall`(없으면 `chef:reviewer-small`)이 맡는다. 그 첫 리뷰 브리프에는 변경 전 본 대비 diff를 넣는다.
작은 리뷰 역할이 뜻·승인 범위 변경, 새 규칙·규칙 관계 변경, 질문·보고 문서나 공통 계약·구조 변경·원인 불명을 발견하면 clean을 선언하지 않고 근거와 남은 범위를 반환한다. 코디네이터는 그 범위를 `roles.review`(없으면 `chef:code-reviewer`)의 심층 리뷰로 확대한다.
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
implementSmall과 verify는 `gpt-6-luna`와 high, 작은 리뷰(`chef:reviewer-small`)는 `gpt-6.1-terra`와 high로 고정한다. 고정값은 등록된 역할 정의(TOML)가 정하며 어댑터 `rolePolicy.model`이 `inherit`여도 정의의 값이 적용된다(아래 역할 정책).
고정 모델·추론 수준을 지정할 수 없으면 다른 모델로 대신 실행하지 않고 코디네이터에게 미확인으로 반환한다.
실행 중 모델 전환·자동 승격은 하지 않는다.
그 밖의 별도 모델은 사용자나 프로젝트 설정이 정한 경우만 쓴다.
읽기 전용은 역할 지시이며 별도 샌드박스를 뜻하지 않는다. 권한은 부모 세션에서 상속한다.

## 역할 정책

어댑터 `rolePolicy`(`runtimes.codex.rolePolicy`가 최상위 값을 덮는다)는 mission 스킬이 역할을 파견할 때만 적용한다. 직접 호출과 TOML의 모델 값은 바꾸지 않는다. 없으면 `model: "definition"`, `subagents: true`다.

- `model: "inherit"` — 등록된 역할은 TOML의 모델이 적용되므로 부모 상속을 지킬 수 없다. 코디네이터가 그 사실을 보고한다.
- `model: "definition"` — 위 고정을 따른다.
- `subagents: false` — `chef:dev-medium`(과 `roles`가 가리키는 같은 계열 역할)의 브리프에 "하위 에이전트를 띄우지 않는다. 나눠야 하면 단위·파일 경계·계약을 담은 분할안을 부모에게 반환한다"를 넣는다. TOML은 기본(허용)을 유지하고 브리프의 금지 지시를 따른다. 도구로 막는 강제 차단이 아니다.

역할 파견은 `codex/agents/*.toml`을 `~/.codex/agents/`에 복사해 등록한 이름이 전제다. 등록하지 않으면 위 규칙대로 사용자에게 알린다.
등록할 때 기존 파일이 있으면 먼저 차이를 보고하고 덮어쓰지 않는다. 이 플러그인 설치만으로 전역 파일을 쓰지는 않는다.
등록한 사본은 사용자가 플러그인을 업데이트할 때 함께 다시 복사한다. chef doctor가 낡은 등록·빠진 역할을 보고한다.
