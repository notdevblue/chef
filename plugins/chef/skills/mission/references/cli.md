# Chef CLI

스킬의 `chef …`는 **현재 세션이 로드한 번들**의 아래 호출을 뜻한다. 훅은 미션 전환과 질문 쓰기에서 현재 번들 경로를 확인한다.

```text
python "<plugin-root>/chef_cli.py" --help
python "<plugin-root>/chef_cli.py" context --runtime <claude|codex|grok|hermes> --session "<현재 세션 id>"
```

프로젝트 명령에 현재 런타임을 `--runtime`으로 지정한다. 다른 폴더를 조회할 때만 `--project`를 붙인다. `context`는 번들·버전·프로젝트·어댑터·기록된 ACTIVE를 JSON으로 반환하며, 세션을 주면 소유자 일치도 반환한다. 미션 안의 진행 요약은 `chef mission context --json`이다. Hermes에서는 native 문맥의 Python 경로를 쓰고 open/resume/close는 `chef_mission`으로 호출한다.

하위 명령의 옵션은 해당 `--help`로 확인한다. 기존 스크립트를 쓰는 자동화도 지원한다. 프로젝트별 검증·VCS 정책과 호스트 권한은 어댑터와 훅의 계약을 따른다.

버전이 다른 번들을 병행할 때는 미션마다 같은 버전의 CLI·훅·역할을 사용한다. 기존 워커 결과를 먼저 수집한 뒤 전환한다. 새 `schema-table`로 발행한 질문은 그 형식을 지원하는 번들의 CLI와 대시보드에서 연다.

## 워커·리뷰

`worker dispatch <단위> --role <역할> --session <현재 세션 ID> --request-id <작업 ID>`는 현재 ACTIVE와 저장된 브리프를 사용한다. 동일한 파견을 재시도할 때는 같은 작업 ID를 쓴다. 범위를 좁히는 `--file`, 추가 읽기 입력 `--input-path`, 관찰한 호스트 역할·도구와 추가 지시는 필요할 때 지정한다. 예외 범위나 파견 설정은 [파견 계약](cross-runtime-workers.md)의 `--request - --stored-brief`에서 명시할 수 있다.

단위 기반 리뷰 파견은 그 단위의 최신 통과 검증에 묶인다. 외부 리뷰 결과는 워커 저장소에 들어오며 native 도구의 반환 JSON은 `worker native-result --key … --token … --result -`에 그대로 전달한다. 코디네이터가 `review <단위> --job <키> --session <현재 세션 ID>`로 기록하면 보고서와 지적 JSON을 Chef가 보관한다. `review-open <단위>`는 직전 열린 지적을 보여 준다. 코디네이터가 Low 해소를 직접 닫는 경우 diff와 재검증 근거를 확인하고 `review --status closed-low --resolved-id <id>`를 사용한다. 수동 리뷰 입력과 지적 형식은 [검증 계약](verification-workflow.md)을 따른다.

## 검증 계획

`plan template <단위>`는 승인된 파일과 선택 가능한 검증 등급으로 초안을 만든다. 검증 명령·환경·필요한 결과 조건을 채워 `plan save <단위> --input -`로 등록한다. `preflight`, `verify`, `review-start`, `review-scope`, `review-run`에서 `--plan`을 생략하면 그 단위의 등록 계획을 사용한다. 교체할 때는 `plan show <단위>`가 반환한 `sha256`을 `--expect-sha256`에 지정한다. 기존 계획을 직접 쓸 때는 `--plan <경로>`를 붙인다. 파일 증거는 `ledger record ... --evidence-input -`로 입력한다.

## 문서

- `mission init`이 기본 보고서 `report.md`의 초안을 만든다. 추가 보고나 상세는 `document create --kind report|detail --name <이름> --title <제목>`으로 만든다.
- `document check <경로...>`는 검사·공유·승인 상태를 반환한다. `document share <경로...> --json`은 검사와 등록을 한 뒤 결과와 바이트 해시를 반환한다. 바뀐 파일은 다시 검토하고 공유한다.
- 질문은 `question template --kind question|design`에서 시작해 `question preflight --preview`로 검사한 뒤 발행한다. 답변은 `question reply-template <문서.md>`로 기록 ID·현재 개정·시각을 가져와 원문과 명시된 결정만 채우고 `question answer <문서.md> --input -`로 저장한다. `preview`, `preflight`, `publish`도 `--input -`를 받는다. 정본·생성본·이력은 `question check`, 원문과 초안 대조는 `document compare`로 확인한다. 복구는 [질문 정본 계약](question-records.md)을 따른다.

### DB·Proto 정의

스키마 표 하나에는 대상 하나만 둔다. 새 확인 A의 본문·상세에는 새 일반 `table`·`fields` 블록을 쓰지 않는다. 데이터 정의는 `schema-table`, DB↔Proto 관계와 비교는 산문이나 목록으로 쓴다. 일반 보고서에서는 스키마 정의를 아래 블록으로 만든 문서에 연결한다. `document create --kind schema --name book --input <JSON>`은 한 블록을 `details/book.md`로 생성한다.

```json
{
  "id": "book",
  "type": "schema-table",
  "title": "Book columns",
  "target": {"kind": "db", "name": "Book", "state": "existing"},
  "fields": [{"name": "Id", "type": "int", "key": "primary"}]
}
```

기존 테이블은 어댑터의 `documents.schemaCatalog`를 사용한다. 제안 테이블은 `target.state: "proposed"`와 프로젝트 상대 경로인 `target.source`를 지정한다. 두 경로의 JSON 형식은 `{"schema":1,"tables":[{"kind":"db","name":"Book","fields":{"Id":"int"}}]}`이다. `target.sha256`을 주면 기록한 카탈로그 버전과 일치해야 한다. 테이블·필드·타입이 불일치하면 발행·공유가 거부된다. 카탈로그를 제공하지 않았거나 없으면 `unverified`로 보고한다. 키·제약·승인 의미는 해당 프로젝트 검토 절차로 확인한다. 프로젝트가 기존 명령에도 새 입력 규칙을 적용할 때는 `documents.requireSchemaTables: true`를 설정한다. 기존 정본과 이력은 자동으로 이관하지 않는다.
