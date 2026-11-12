# 검증과 완료 처리

공통 계획·preflight·공유 자원·receipt·리뷰 계약은 [공통 검증 절차](../../../../skills/mission/references/verification-workflow.md)를 따른다.
어댑터 `verify.resourceDir`·`resources`·`readiness`의 스키마는 [Codex 어댑터](adapter-schema.md)의 `verify.*`를 따른다.

Codex는 `chef_verifier`의 GPT-6 Luna / high로 고정한다. 등록되지 않은 Codex 역할은
`codex-roles.md`의 모델·추론 수준과 지시를 따른다. 어댑터 `rolePolicy.model`이 `inherit`면 미등록 파견은 부모를 상속하고,
등록된 역할은 TOML 정의를 따른다.

검증 명령은 변경된 경로·계약을 실제로 덮어야 한다. 프로젝트 전역 실패는 별도 게이트로 유지하고,
약한 타깃 검사로 대체하거나 범위를 우회하지 않는다. 요청한 각 선택자가 실제 카탈로그나 결과에서 해석·선택되고,
요청 범위가 실행됐다는 증거를 확인한다. 직접 실행 목록 또는 프로젝트 실행 계약이 선택자와 실행 결과를 연결하는 근거를 쓴다. 근거가 없거나 부족하면 통과로 판단하지 않고 미확인으로 반환한다.
카탈로그에서 선택된 사실이나 양수 전체 실행 수만으로는 실제 실행을 입증하지 못한다. 클래스·매개변수화 사례는
요청된 의미 범위의 실행 근거가 필요하다. 필터 선택 수와 실행 수는 일반적으로 다르다.
프로젝트가 선택·실행 범위를 결과에 기록하면 공통 `results[].equals`·`results[].positive`로 검사한다.

계획에 `referenceBundle`을 넣으면 `preflight <단위> --plan <계획.json>`이 먼저 참조·준비 상태를 확인한다.
preflight 통과 뒤 `verify <단위> --plan <계획.json>`을 실행한다. 참조 원문이 바뀌면 코디네이터가 승인 범위를 확인하고 묶음을 갱신한다.
계획·receipt를 미리 훑을 때는 공유 `mission_state.py inspect --plan <경로>` 또는 `inspect --receipt <경로>`를 쓴다. `--plan`·`--receipt` 중 하나만 지정하며 mission·adapter·ACTIVE를 탐색하지 않는다. 원본을 변경하거나 포인터 대상 파일을 열지 않고 `source`·`identity`·`counts`·`steps`·`statusCounts`·`failedSteps`·`failure.repeatCount`·`executionCounts`·`recorded` 포인터만 요약한다. `current_validity=not_checked`는 현재 유효성이나 재사용 보증이 아니므로 실제 판정은 아래 기존 `preflight`·`verify`로 한다.
