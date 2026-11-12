# Claude 사용량 측정


`scripts/usage_report.py`는 Claude JSONL을 읽는 로컬 명령이다. 모델 호출·상태 변경 없이 프로젝트별,
역할별, 모델·effort별 입력·캐시 생성·캐시 읽기·출력과 별도 Advisor 사용량을 JSON으로 저장한다.
보고서에는 프롬프트·응답·암호화된 Advisor 본문을 넣지 않는다.

```text
python <plugin>/skills/mission/scripts/usage_report.py --project main=D:/Work/Game --project slot1=D:/Work/slots/slot1 --start 2026-10-23T00:00:00+09:00 --end 2026-10-24T00:00:00+09:00 --output usage.json
```

`--start` 포함·`--end` 제외이며 시간대가 필수다. 기본 설정 디렉터리는 `CLAUDE_CONFIG_DIR` 또는
`~/.claude`다. `--claude-home`으로 바꿀 수 있다. `--mission-root 이름=경로`는 해당 프로젝트의 미션
루트를 지정하며 반복할 수 있다. 생략하면 어댑터의 `paths.stateDir`을 찾는다. 없는 경로는 보고서에 표시한다.

중복 스트리밍 응답은 requestId·message.id로 합치고 각 토큰 필드의 최댓값을 쓴다. 별도 Advisor iteration은
일반 메시지 합계와 분리한다. 알 수 없는 iteration·손상된 줄·누락 필드는 진단에 남긴다.
단순 합계는 요금이나 구독 한도 소비율이 아니다. 캐시 읽기를 신규 입력과 같은 비용으로 계산하지 않는다.
실행 중 로그에는 같은 응답의 usage가 뒤늦게 추가될 수 있다. 기간과 보고서 생성 시각을 함께 보존하며,
동일 기간의 재집계 값이 바뀌는 것을 절감·증가 효과로 해석하지 않는다.

리뷰 기록에는 `review --findings-count N --fixed-count N`을 사용한다. 전자는 해당 라운드의 남은 지적,
후자는 직전 지적 중 해소를 확인한 수다(`closed-low`의 `--findings-count`는 0 또는 생략). 기록되지 않은 값은 알 수 없음으로 남기며 고유 버그 수로 합산하지 않는다.
보고서는 단위별 리뷰 횟수·상태·지적 수와 ledger의 명시적 retry 기록을 함께 보여 준다.
기존 타임스탬프 없는 리뷰는 파일 수정 시각 기준임을 표시한다. retry 기록은 전체 재작업 횟수의 대용이 아니다.

작업 종류별 측정은 `--activity-map <JSON>`의 명시적 메타데이터만 쓴다. 형식은 `schema: 1`,
`activities` 배열이며 각 항목에 `sessionId`, 선택적 `agentId`, `taskKind`와 선택적 `mission`, `unit`,
`phase`, `attempt`, `startedAt`, `endedAt`을 둔다. 실제 세션·에이전트 식별자와 시간 구간으로 연결한다.
`taskKind`는 `implementation|document|review|verification|coordination|research`다. `agentId` 생략·빈 값은 루트 세션이며
나머지 생략 메타데이터는 알 수 없음이다. 시각에는 시간대를 넣고 시작 포함·끝 제외로 연결한다.
코디네이터는 파견·반환 시 확보한 실제 세션·에이전트 id와 작업 종류·단위·시도·시각을 기존 미션 기록에 남기고
그 기록으로 로컬 activity-map을 만든다. 수동 보충도 실제 식별자와 실행 근거가 있을 때만 한다.
프롬프트 본문으로 작업 종류를 추측하지 않는다. 매핑 누락·충돌과 미분류 사용량은 알 수 없음으로 보존하며
`activityCoverage`의 assigned·unknown·ambiguous·conflict와 출처 수로 분류된 범위와 전체 대비 공백을 함께 보고한다.

동일 범위·비슷한 작업량의 전후 보고서를 비교한다. 모델·effort 변경, 리뷰·검증 횟수, 지적·수정 확인,
재작업 기록과 미확인 공백을 함께 보고 절감 효과를 판단한다. 정확한 계정 한도는 `/usage`로 별도 확인한다.
