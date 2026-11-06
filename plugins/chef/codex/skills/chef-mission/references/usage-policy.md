# 파견 입력과 참조

Codex 역할과 모델·effort 배정은 [Codex 역할](codex-roles.md)을 따른다. 공통 참조 묶음·brief·handoff 계약은
[공통 파견 정책](../../../../skills/mission/references/usage-policy.md)을 따른다.
Claude 런타임의 모델·effort 배정이나 도구 규칙을 Codex 역할에 적용하지 않는다.

코디네이터는 `reference snapshot --source <자료> --output <묶음.json>`으로 새 참조 묶음을 만든다. 새 출력에는 `--expect-sha256`을 지정하지 않는다.
기존 묶음을 갱신할 때만 직전 snapshot 응답의 `output.sha256`을 `--expect-sha256 <SHA256>`으로 넘긴다.
현재 파일 해시가 다르거나 출력이 참조 묶음이 아니면 덮어쓰지 않는다.
`reference check --input <묶음.json>` 통과 후 같은 묶음을 구현자와 첫 리뷰어에게 전달한다.
`brief <단위> --references <묶음.json>` 출력으로 단위 계약을 전달한다.
반환 상세·증거는 기존 프로젝트 파일로 받고 `handoff <단위> --summary <결과> --detail <상세> [--evidence <근거>]…`로 연결한다.
