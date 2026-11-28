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

`worker dispatch --request - --stored-brief`는 표준 입력의 요청을 접수하고 저장된 단위 브리프를 본문에 넣는다. 승인된 작업 단위와 실제 허용 파일·입력 경로·호스트 역할은 기존 [파견 계약](cross-runtime-workers.md)을 따른다. 이번 역할의 차이만 선택적인 `prompt`에 쓴다. `worker native-result --key … --token … --result -`는 표준 입력에서 구조화된 결과를 받는다. 호스트가 표준 입력을 제공하지 않으면 JSON 파일을 사용한다. Chef의 내부 요청·결과와 근거 파일은 보존된다.

첫 리뷰부터 열린 지적의 `id`, `kind`, `origin`, `severity`, `reason`, `evidence`(근거 경로와 위치)를 사람용 보고와 함께 반환한다. `review --status findings --findings-input -`에서 JSON을 기록하고 후속 확인은 `review-open <단위>`로 직전 열린 지적을 읽는다. 허용된 Low 해소는 코디네이터가 diff와 검증 근거를 확인하고 `review --status closed-low --resolved-id <id>`를 지적마다 반복 지정해 기록한다. 다른 지적과 재리뷰는 [검증 계약](verification-workflow.md)을 따른다.

## 문서

- `document create --kind report|detail --title <제목> [--name <이름>]`으로 초안을 만든다. 같은 경로는 덮어쓰지 않는다.
- `document check <경로...>`는 체크 결과와 현재 공유·승인 상태를 JSON으로 반환한다. `document share <경로...>`는 같은 파일 내용으로 검사하고 해시를 등록한다. 바뀐 파일은 다시 검토하고 공유한다.
- 질문은 `question preflight → preview → publish`로 만든다. 답·승인은 `question answer`로 기록한다. 정본·생성본·이력이 일치하는지는 `question check`로 확인한다. 구형 원문 복구·변환은 [질문 정본 계약](question-records.md)을 따른다.

### DB·Proto 정의

스키마 표 하나에는 대상 하나만 둔다. 새 확인 A의 본문·상세에서 컬럼 정의에는 `schema-table`을 쓴다. DB↔Proto 관계와 비교는 별도 설명에 둔다. 일반 보고서에서는 스키마 정의를 아래 블록으로 만든 문서에 연결한다. `document create --kind schema --name book --input <JSON>`은 한 블록을 `details/book.md`로 생성한다.

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
