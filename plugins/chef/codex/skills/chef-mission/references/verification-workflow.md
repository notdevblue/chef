# 검증과 완료 처리

공통 계획·preflight·공유 자원·receipt·리뷰 계약은 [공통 검증 절차](../../../../skills/mission/references/verification-workflow.md)를 따른다.
어댑터 `verify.resourceDir`·`resources`·`readiness`의 스키마는 [Codex 어댑터](adapter-schema.md)의 `verify.*`를 따른다.

Codex는 `chef_verifier`의 GPT-6 Luna / high로 고정한다. 등록되지 않은 Codex 역할은
`codex-roles.md`의 모델·추론 수준과 지시를 따른다. 어댑터 `rolePolicy.model`이 `inherit`면 미등록 파견은 부모를 상속하고,
등록된 역할은 TOML 정의를 따른다.

계획에 `referenceBundle`을 넣으면 `preflight <단위> --plan <계획.json>`이 먼저 참조·준비 상태를 확인한다.
preflight 통과 뒤 `verify <단위> --plan <계획.json>`을 실행한다. 참조 원문이 바뀌면 코디네이터가 승인 범위를 확인하고 묶음을 갱신한다.
