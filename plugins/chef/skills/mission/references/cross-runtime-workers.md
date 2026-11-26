# Claude·Codex·Grok 역할 워커

Claude Code·Codex CLI·Grok CLI의 미션 코디네이터는 역할을 직접 배정하지 않고 공통 워커 라우터로 파견한다. 라우터는 사용할 수 있는 CLI·인증, 역할과 필요한 도구를 먼저 확인한다. 수정 후속 작업은 이전 작업의 안정적인 job key를 `previous`로 넘긴다. 유효한 세션 ID와 같은 역할 설정이 남아 있으면 그 런타임의 세션을 이어가고, 독립 리뷰는 새 맥락에서 시작한다. 동일 역할·조건으로 후보마다 독립 검증된 표본이 5개 이상 있고 성공률이 20%p 이상 높거나 재작업률이 작업당 0.5회 이상 낮을 때만 품질을 선택에 반영한다. 품질 기록은 미션 완료·최신 검증·독립 리뷰·결과 파일 해시가 모두 유효하고, 실제 관찰 모델·effort도 선택된 값과 일치해야 한다. 그 밖에는 진행 중인 작업이 적은 런타임을 우선하고, 동률이면 순환 선택한다.

선택된 런타임이 현재 세션과 같으면 해당 런타임의 native 역할 도구로 파견한다. 다른 런타임이면 공통 CLI가 별도 프로세스에서 실행하며 각 서비스의 로그인·인증을 사용한다. 같은 Python CLI를 Windows·macOS·Linux에서 쓴다. 동시 실행 수를 제한하지 않는다. 도구나 인증을 사용할 수 없는 런타임은 후보에서 제외한다. 역할과 도구 요구를 만족하는 후보가 없으면 임의 대체하지 않고 미완료로 보고한다.

외부 런타임에서 작업 전 probe 중 서비스 rate limit을 명확히 받으면 파일 변경이 없고 자식 프로세스가 정리된 경우에만 다른 eligible runtime으로 자동 전환한다. 한 요청에서 후보 런타임을 각각 한 번 시도한다. 짧은 로컬 재시도 backoff는 60초에서 최대 900초이며, 이는 서비스의 quota reset 시간을 뜻하지 않는다. 실행 중 오류, 불확실한 실패 또는 파일 변경이 있으면 자동으로 다른 런타임에 넘기지 않는다. 자동 전환의 목적지는 현재 세션일 수도 있다.

외부 Grok 워커는 `Read`·`Grep`·`Glob`만 받고 shell·MCP·직접 파일쓰기 도구를 받지 않는다. 구현 후보에는 논리적 `Write`·`Edit`·`Delete` 역량이 있지만, Grok은 이를 도구로 실행하지 않고 사전 해시가 포함된 변경 JSON을 반환한다. `content: null`은 기존 파일 삭제이며 이름 변경은 허용 범위 안의 삭제·생성 두 항목으로 표현한다. Chef는 모든 변경의 경로·사전 해시·쓰기 권한·미션 가드를 확인하고, 적용 직전에도 파일 해시와 소유권을 다시 확인한 뒤 원자적으로 기록한다. 바이너리 변경은 지원하지 않는다. 적용 도중 쓰기가 실패하면 일부 파일만 바뀔 수 있으므로 완료로 처리하지 말고 저널과 현재 파일을 확인한다. 이 절차도 OS sandbox를 대신하지 않는다.

| 작업 상황 | 선택 동작 |
| --- | --- |
| Claude 미션의 구현 뒤 같은 단위를 수정 | 이전 job key가 유효하면 같은 런타임 세션을 이어간다. 역할 정책이 바뀌었거나 세션 증거가 없으면 새 후보 선택으로 돌아간다 |
| 독립 리뷰 | `independent: true`로 이전 세션을 쓰지 않는다. 검증된 품질 차이가 충분할 때 반영하고, 아니면 진행 중 작업량과 순환 순서로 고른다 |
| 단위 검증에 현재 호스트 전용 MCP 필요 | 그 도구가 관찰된 native 런타임만 후보가 된다. native 역할도 등록되어 있지 않으면 작업을 맡기지 않고 미완료로 보고한다 |
| 외부 Grok에 코드 변경 파견 | 요구 도구의 논리적 `Write`·`Edit` 역량을 확인한다. Grok은 제안 JSON을 만들고 Chef가 지정 경로·사전 해시를 검증해 적용한다. Shell 검증은 맡기지 않는다 |

## 파견 계약

미션 상태와 ACTIVE 소유권을 확인한 뒤 단위 브리프·승인된 파일 범위·필요한 도구를 정해 아래 JSON을 만든다. 파일 경로는 `project` 기준 상대 경로다. `inputPaths`는 실행 전후 입력 동일성을 검사할 경로이고 `files`는 수정 허용 범위다. 조사·리뷰는 `files`가 비어 있을 수 있다. 구현·검증은 파일 범위를 지정한다. `requiredTools`에는 역할에 실제로 필요한 도구만 넣는다. `hostTools`와 `hostRoles`에는 현재 native 세션에서 확인한 도구·등록 역할 이름만 쓴다. 플러그인에 기본 역할 파일이 있다는 사실만으로 native 역할이 등록됐다고 판단하지 않는다. MCP처럼 현재 호스트에만 연결된 도구는 다른 런타임에서 사용할 수 있다고 가정하지 않는다.

```json
{
  "project": "/absolute/project/path",
  "ownerRuntime": "codex",
  "ownerSession": "current-session-id",
  "mission": "existing-mission-id",
  "unit": "M2-U3",
  "requestId": "M2-U3-review-1",
  "hostRoles": ["chef:reviewer-small"],
  "role": "reviewer-small",
  "prompt": "The complete approved role brief",
  "inputPaths": ["src/change.py", "tests/test_change.py"],
  "files": [],
  "requiredTools": ["Read", "Grep"],
  "hostTools": [],
  "condition": "small-review",
  "previous": null,
  "independent": true,
  "timeout": 600
}
```

요청을 임시 JSON 파일에 저장한 뒤 실행한다.

```text
python "<plugin-root>/worker.py" dispatch --request "<request.json>"
```

요청은 등록된 프로젝트 루트와 `ownerRuntime`·`ownerSession`이 현재 ACTIVE 소유자와 일치하는 초기화된 미션에 결부된다. 미션을 열거나 이어받지 않으며 승인이나 완료 상태도 바꾸지 않는다. `requestId`는 논리적 역할 작업마다 필요한 안정된 고유 ID다. 같은 ID·요청을 재전송하면 기존 작업을 반환하고, 같은 ID로 내용이 달라지면 거부한다. 이전 결과를 확인하고 새 시도를 만들 때는 새 ID를 쓴다. 수정 후속 작업은 이전 job key를 `previous`에 넣는다. 관찰한 세션 ID와 역할 정책이 그대로인 경우에만 워커 세션을 재개한다. 독립 리뷰에는 `independent`를 지정해 새 맥락을 사용한다.

## 결과 수집

`dispatch`는 job `key`, 시도 `token`, 선택 경로, 상태와 `native` 여부를 반환한다. `native`가 true면 반환된 `actualRole` 이름으로 native 역할 도구를 실행한다. 끝난 뒤 `{"ok":true,"result":"summary","evidence":["reports/review.md"]}`처럼 결과 JSON을 저장해 기록한다.

```text
python "<plugin-root>/worker.py" native-result --key "<key>" --token "<token>" --result "<result.json>"
```

성공한 native 결과에는 실제 존재하는 프로젝트 상대 근거 파일이 있어야 한다. 결과 기록만으로 미션 단위를 완료하지 않는다. 코디네이터가 기존 절차대로 결과를 검토하고 단위 상태를 갱신한다.

`native`가 false면 별도 supervisor가 `dispatch` 종료 뒤에도 작업을 이어간다. 다음 명령은 로컬 상태만 확인하며 모델을 반복 호출하지 않는다. 작업 전 rate limit으로 native 세션에 handoff될 수 있으므로 반환된 레코드의 `native`를 매번 확인한다. true가 되면 `actualRole`·`brief`를 native 도구로 실행하고 결과를 기록한다. 완료된 결과와 근거를 확인한 뒤 작업에 반영한다.

```text
python "<plugin-root>/worker.py" wait --key "<key>" --seconds 30
```

`status --key <key>`는 즉시 반환하는 로컬 조회다. 새 소유자 세션으로 미션을 재개했으면 ACTIVE 소유자를 먼저 확인한 뒤 `recover-owner`로 기존 작업 결과의 접근 소유권을 바꾼다. 이 명령은 작업을 다시 실행하지 않는다. 이전 소유자에 연결된 실행 중 워커는 소유권 검사에서 멈춘다.

```text
python "<plugin-root>/worker.py" recover-owner --key "<key>" --token "<token>" --runtime "codex" --session "resumed-session-id"
```

취소가 필요할 때 반환된 시도 token으로 요청한다. 워커가 멈춘 뒤 부분 변경을 확인한다. native 작업은 역할 도구를 취소하고 결과를 수집한 뒤 취소 결과를 기록한다.

```text
python "<plugin-root>/worker.py" cancel --key "<key>" --token "<token>"
```

중단된 워커가 프로세스나 부분 수정을 남길 수 있다. 추적 중인 프로세스가 모두 끝나고 출력 변경을 확인할 때까지 재시도하거나 파일 범위를 풀지 않는다. 이전 작업을 정리하려면 프로젝트 상대 근거 경로를 지정한다. `reconcile`은 관찰한 해시와 현재 출력 파일 상태를 기록하고 기존 시도를 실패로 닫지만 수정 내용을 되돌리지는 않는다. 후속 시도는 새 `requestId`를 쓴다.

```text
python "<plugin-root>/worker.py" reconcile --key "<key>" --token "<token>" --evidence "reports/inspection.md"
```

상태 저장소 기본 경로는 `~/.chef/workers`다. `CHEF_WORKERS_HOME` 또는 명령 앞에 쓰는 전역 `--home` 옵션으로 바꿀 수 있다. 이 저장소는 같은 컴퓨터의 Chef 세션을 조정하는 로컬 기록소이며 원격 큐가 아니다. 자체 프로세스는 POSIX process group 또는 Windows Job Object로 관리한다. POSIX에서 해당 그룹을 벗어난 프로세스는 종료 보장에 포함되지 않는다. 도구·파일 범위 검사는 협력형 가드이며 일반 OS 파일시스템 sandbox가 아니다. 결과를 수용하기 전에 diff와 근거를 확인한다. `needs-inspection`은 부분 변경을 reconcile한 뒤에만 재시도한다. 라우터는 계정 할당량·잔여 크레딧·과금을 추산하지 않는다. 외부 워커는 각 런타임의 로그인이 필요하며 서비스 가용성이나 계정 정책에 따라 실패할 수 있다.
