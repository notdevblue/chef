# 파견 입력과 참조

Codex 역할과 모델·effort 배정은 [Codex 역할](codex-roles.md)을 따른다. 공통 참조 묶음·brief·handoff 계약은
[공통 파견 정책](../../../../skills/mission/references/usage-policy.md)을 따른다.
Claude 런타임의 모델·effort 배정이나 도구 규칙을 Codex 역할에 적용하지 않는다. 공통 정책의 `chef:reviewer-small`은 Codex에서 `chef_reviewer_small`이다.

코디네이터는 `reference snapshot --source <자료> --output <묶음.json>`으로 새 참조 묶음을 만든다. 새 출력에는 `--expect-sha256`을 지정하지 않는다.
기존 묶음을 갱신할 때만 직전 snapshot 응답의 `output.sha256`을 `--expect-sha256 <SHA256>`으로 넘긴다.
현재 파일 해시가 다르거나 출력이 참조 묶음이 아니면 덮어쓰지 않는다.
`reference check --input <묶음.json>` 통과 후 같은 묶음 하나를 구현·수정·첫 리뷰·재리뷰 브리프가 모두 가리킨다.
`brief <단위> --references <묶음.json>` 출력으로 단위 계약을 전달한다.
비용이 큰 구현·검증 파견 전에 코디네이터는 실제 대상 경로, 파일·자원 소유와 허용 편집 범위,
문서화된 명령·API 사용법, 변경 파일·계약을 덮는 검증 명령을 확인해 구체적 근거를 브리프에 넣는다.
확인은 찾을 수 있는 자료에 한정하며 경로·문법·검증을 추측하지 않는다.
반환 상세·증거는 기존 프로젝트 파일로 받고 `handoff <단위> --summary <결과> --detail <상세> [--evidence <근거>]…`로 연결한다.
구현자는 첫 구현에서 변경 함수·블록 또는 문서 전체를, 지적 수정에서는 바뀐 줄과 직접 영향 범위를 대조한다. 반환에는 대조 범위·자료의 실제 경로와 절·기계 검사 결과·미확인 또는 제외 범위와 이유를 간결히 적고, 일부 확인을 전체 준수처럼 보고하지 않는다. 자기 확인은 기계적 준수 증명이나 독립 리뷰를 대신하지 않는다. 공개 플러그인에는 회사·고객·비공개 프로젝트 규칙·사례를 복사·요약·익명화하지 않는다.
계획·receipt 조회는 공유 `mission_state.py inspect --plan <경로>` 또는 `inspect --receipt <경로>`를 우선한다. 둘 중 하나만 지정하며 `source`·`identity`·`counts`·`steps`·`statusCounts`·`failedSteps`·`failure.repeatCount`·`executionCounts`와 `recorded` 포인터 중 필요한 요약만 사용한다. 원본 JSON·로그 전체는 컨텍스트에 싣지 않는다. `current_validity=not_checked`는 현재 입력·정책·환경이나 재사용 판정이 아니며, 검증·재사용은 공통 `preflight`·`verify` 절차가 맡는다.
취소·중단 상태만으로 이미 도착한 같은 task 결과를 버리지 않는다. 재파견 전 최종 본문·근거 파일·실제 완료 범위·입력 상태를 확인해 유효한 결과는 재사용하고 미완료 부분만 후속 파견한다. 결과가 없거나 입력에서 무효하면 다시 실행하며, `cancelled`는 clean을 뜻하지 않는다.
