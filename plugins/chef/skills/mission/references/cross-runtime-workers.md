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

## 자동 배정 켜기·끄기

| 모드 | 파견할 서비스 |
| --- | --- |
| `auto` (기본) | Claude·Codex·Grok 중 자동 선택 |
| `native` | 파견 요청을 만든 현재 서비스만 사용. Claude 세션이면 Claude만 사용 |
| `claude` / `codex` / `grok` | 지정한 서비스만 사용. 현재 세션과 다르면 해당 CLI로 실행 |

사용자가 “이번 미션은 Claude만”, “자동 배정 꺼”, “다시 자동 배정 켜”라고 하면 코디네이터는 각각 `claude`, `native`, `auto`로 해석하고 적용 모드를 짧게 알린다. 해당 미션의 이후 **모든 역할 요청**에 `mode`를 전달하며 수정·검증·리뷰에도 유지한다. 이어받을 때에는 미션 기록과 대화에 남긴 사용자 선택을 먼저 확인한다. 모델 이름을 물을 필요는 없다. 현재 서비스만 쓰는 모드에서도 구현과 독립 리뷰의 맥락 분리는 유지한다. 이 옵션은 미션 역할 파견에 적용하며 현재 코디네이터 자체의 서비스를 바꾸지는 않는다. 미션 없는 직접 작업은 기존 절차를 따른다.

모든 프로젝트에 쓸 전역 기본값은 `~/.chef/config.json`에 설정한다. `~`는 Windows·macOS·Linux 각각 현재 사용자의 홈 디렉터리다. 예를 들어 자동 배정을 끄고 현재 서비스만 쓰려면 아래처럼 둔다. `auto`로 바꾸면 다시 켜며, `claude`·`codex`·`grok`으로 특정 서비스를 고정할 수도 있다. 파일이 없으면 기본값은 `auto`이고 조회만으로 파일을 만들지 않는다. 잘못된 JSON·형식·모드는 사용 전에 오류로 알리며 자동 배정으로 조용히 되돌리지 않는다. 다른 전역 설정 키는 보존한다.

```json
{"workers": {"mode": "native"}}
```

프로젝트에서만 다르게 쓸 기본값은 어댑터 또는 개인 `local.json`의 같은 `workers.mode`에 설정한다. 기존 `workers`의 다른 항목은 보존한다. 이 필드는 Claude·Codex·Grok 공통 dispatcher에 적용하며 Hermes의 기존 backend 선택에는 적용하지 않는다.

실행별로는 `dispatch --mode native`를 쓰거나 요청 JSON에 `"mode": "native"`를 넣는다. CLI 옵션 > 요청 `mode` > `CHEF_WORKERS_MODE` 환경변수 > 어댑터 `workers.mode` > 전역 `~/.chef/config.json`의 `workers.mode` > `auto` 순으로 적용한다. 허용 서비스·역할 규칙을 함께 적용하므로 전역 파일은 모드 override가 있어도 매 파견 전에 읽고 검증한다. 잘못된 전역 JSON을 무시해 제외 서비스를 다시 허용하지 않는다. 환경변수를 쓰면 같은 환경에서 시작한 Chef 세션의 기본값이 된다.

```powershell
$env:CHEF_WORKERS_MODE = "native"
```

```sh
export CHEF_WORKERS_MODE=native
```

`auto`로 바꾸면 자동 배정을 다시 켠다. 환경변수 설정은 서비스 CLI를 시작하기 전에 한다. 단일 서비스 모드에서는 다른 서비스의 인증·역할 검사도 하지 않고, quota·도구 부족·인증 실패 때 다른 서비스로 넘어가지 않는다. 허용 서비스 안에서 실행할 수 없으면 이유를 보고한다. 응답의 `mode`와 `allowedRuntimes`로 적용 모드와 해당 역할의 최종 후보 범위를 확인한다.

모드는 작업 접수 시 저장되며 이미 접수한 작업을 취소하거나 재파견하지 않는다. 전역 파일 변경은 다음 파견부터 반영하며 기존 작업은 접수 당시 모드를 유지한다. 모드 변경 뒤 새 작업은 새 `requestId`를 사용한다. 실행 중 어댑터를 수정하면 기존 설정 동일성 검사로 워커가 멈출 수 있으므로, 진행 중 전환에는 전역 파일 또는 이후 요청의 `mode`·`--mode`를 사용한다. 미션별 자연어 선택은 코디네이터가 이후 요청에 전달하는 선택이며 별도 미션 상태 필드를 자동 수정하지 않는다.

## 허용 서비스와 역할별 고정

일부 서비스만 자동 배정에 쓰려면 `workers.runtimes`를 지정한다. 생략하면 세 서비스를 모두 허용한다. 비어 있는 목록·중복·지원하지 않는 서비스 이름은 오류다. 제외한 서비스는 역할·인증 확인과 rate-limit 대체 후보에서도 제외한다. 동시 실행 수를 제한하는 옵션은 아니다.

역할별 고정은 `workers.roleRouting`에 정규 역할 이름과 `runtime`을 적는다. 예를 들어 아래 전역 설정은 Claude·Codex 안에서 조사·구현·검증을 자동 배정하고 첫 리뷰·재리뷰는 Claude로 고정한다.

```json
{
  "workers": {
    "mode": "auto",
    "runtimes": ["claude", "codex"],
    "roleRouting": {
      "code-reviewer": {"runtime": "claude"},
      "reviewer-small": {"runtime": "claude"}
    }
  }
}
```

역할 키는 `researcher`, `dev-small`, `dev-medium`, `verifier`, `code-reviewer`, `reviewer-small`이다. 사용자 정의 native 역할 이름이 있어도 이 논리적 역할 키로 지정한다. 요청의 `review`·`audit`은 `code-reviewer`, `reviewSmall`은 `reviewer-small` 규칙을 사용한다. 고정할 서비스는 `claude`·`codex`·`grok`이며, `runtime: "auto"`는 상속한 해당 역할의 고정을 풀고 허용 서비스 안의 자동 선택으로 돌린다. 역할별 모델·effort 설정은 기존 역할 정의를 따른다.

`runtimes`는 요청 > 프로젝트 어댑터 > 전역 설정 순으로 선택하며 상위 목록이 하위 목록 전체를 대체한다. `roleRouting`은 전역 → 프로젝트 → 요청 순으로 **역할별 항목을 덮어쓴다**. 지정하지 않은 역할의 전역 규칙은 유지한다. 요청에서 직접 지정할 때에는 `runtimes`와 `roleRouting`을 요청 최상위에 쓴다. 명시적 사용자 선택이 없으면 코디네이터가 전체 목록이나 빈 규칙을 임의로 주입하지 않는다.

실행 범위는 허용 서비스와 모드, 역할별 고정의 교집합이다. 전체 설정의 역할 고정이 허용 목록 밖의 서비스를 가리키거나, `native`·단일 서비스 모드와 다른 서비스를 고정하면 어떤 역할도 파견하기 전에 설정 오류로 알린다. 예를 들어 Codex 세션에서 `native`와 Claude 리뷰 고정을 함께 쓰려면, 먼저 리뷰 규칙을 `runtime: "auto"`로 풀거나 모드를 `auto`로 바꿔야 한다. 상위 모드 옵션만으로 제외 서비스나 역할 고정을 조용히 무시하지 않는다.

고정 리뷰가 quota·도구·인증 문제로 실행되지 않으면 다른 서비스에 넘기지 않고 실패 이유를 보고한다. 주기적인 모델 재호출로 기다리지 않는다. 독립 리뷰는 같은 서비스에서도 새 맥락을 유지한다. 선택된 정책과 최종 허용 목록은 접수 시 작업에 저장하며, 설정 변경이나 rate-limit 대체로 진행 중 작업의 범위를 넓히지 않는다. 후속 작업의 `previous` 런타임도 새 정책에서 허용될 때만 재개 후보가 된다.

## 파견 계약

승인된 단위를 파견할 때는 `chef worker dispatch <단위> --role <역할> --runtime <런타임> --session <현재 세션 ID> --request-id <작업 ID>`를 먼저 쓴다. 구현 파일은 브리프 `files`의 기존 파일 하나 또는 정확한 프로젝트 상대 경로의 JSON 배열에서 읽는다. 배열은 승인된 새 파일도 담을 수 있다. `--file`로 그 범위 안의 파일만 선택할 수 있다. `--input-path`는 추가 읽기 경로, `--previous`는 같은 역할의 후속 작업, `--prompt`는 해당 파견에 필요한 추가 지시를 전달한다. 검증 역할에는 그 단위에 등록된 계획 파일 경로와 해시를 연결한다. 독립 리뷰는 최신 통과 receipt에 묶이고 이전 워커 세션을 이어가지 않는다.

추가 계약을 명시해야 할 때는 아래 JSON을 `--request`로 넘긴다. `inputPaths`는 실행 전후 동일성을 검사할 경로이고 `files`는 구현에 허용된 정확한 수정 범위다. 검증 실행 권한은 검증 계획에 묶인다. `requiredTools` 또는 `--required-tool`에는 실제로 필요한 도구만 넣는다. `hostTools`·`hostRoles` 또는 `--host-tool`·`--host-role`에는 현재 native 세션에서 확인한 도구와 등록 역할만 전달한다. 어댑터의 논리적 역할과 실제 등록 이름은 다를 수 있다. 호스트 전용 도구를 다른 런타임에서 요구하면 해당 후보는 제외된다.

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

`files`는 정확한 수정 파일 목록이며 아직 없는 새 파일도 지정할 수 있다. `inputPaths`는 파일과 디렉터리를 받으며 디렉터리는 최대 10,000개 파일의 내용을 고정한다. 프로젝트 내부를 가리키는 읽기 링크는 허용하고 외부로 나가는 링크는 거부한다. 쓰기 링크·정책 메타데이터·미션 제어 파일의 수정 권한은 부여하지 않는다.

요청의 `actualRole`로 현재 단위에만 실제 역할을 선택할 수 있다. 어댑터의 해당 역할 또는 같은 역량의 기본 `chef:<role>`만 허용하고 `agents.allow`도 적용한다. 호스트의 실제 등록은 `hostRoles`로 별도 확인한다. 기본 어댑터나 다른 단위는 변경하지 않는다.

검증 요청은 `role: verifier`, `files: []`, `verificationPlan: <owned mission plan>`으로 만들 수 있다. 계획 경로·해시·단위를 작업에 결부하며 외부 검증자는 이 계획의 정식 verify 호출만 예외적으로 실행할 수 있다. 질문·승인·완료 등 다른 미션 상태 변경 권한은 주지 않는다. 결과 파일은 검증 실행기가 관리하며 소스 수정 파일을 억지로 결과 범위 대신 넣지 않는다.

`timeout`은 기본 600초이며 프로젝트 `workers.maxTimeoutSeconds`(기본 7200, 설정 범위 10~86400초) 안에서 정한다. 개별 검증 명령의 시간 제한과 전체 워커 예산은 별도다.

요청은 표준 입력 JSON으로 전달할 수 있다. `--stored-brief`는 저장된 단위 브리프를 사용하고 `prompt`는 역할별 추가 지시로만 붙인다.

```text
chef worker dispatch --request - --stored-brief
```

요청은 등록된 프로젝트 루트와 `ownerRuntime`·`ownerSession`이 현재 ACTIVE 소유자와 일치하는 초기화된 미션에 결부된다. 미션을 열거나 이어받지 않으며 승인이나 완료 상태도 바꾸지 않는다. `requestId`는 논리적 역할 작업마다 필요한 안정된 고유 ID다. 같은 ID·요청을 재전송하면 기존 작업을 반환하고, 같은 ID로 내용이 달라지면 거부한다. 이전 결과를 확인하고 새 시도를 만들 때는 새 ID를 쓴다. 수정 후속 작업은 이전 job key를 `previous`에 넣는다. 관찰한 세션 ID와 역할 정책이 그대로인 경우에만 워커 세션을 재개한다. 독립 리뷰에는 `independent`를 지정해 새 맥락을 사용한다.

## 결과 수집

`dispatch`는 job `key`, 시도 `token`, 선택 경로, 상태와 `native` 여부를 반환한다. `native`가 true면 반환된 `actualRole` 이름으로 native 역할 도구를 실행한다. 끝난 뒤 `{"ok":true,"result":"summary","evidence":["reports/review.md"]}`처럼 결과를 표준 입력에 전달한다.

```text
chef worker native-result --key "<key>" --token "<token>" --result -
```

호스트가 표준 입력을 전달할 수 없으면 파일을 `--request` 또는 `--result`에 지정한다. 성공한 native 결과에는 실제 존재하는 프로젝트 상대 근거 파일이 있어야 한다. 코디네이터가 결과를 검토하고 단위 상태를 갱신한다.

업데이트 전 요청도 저장된 key·token으로 수집한다. 저장소·요청 파일이나 기존 설치 경로를 고쳐 재접수하지 않는다. 접수 뒤 출력이나 입력 경로가 폴더·링크 등으로 바뀌거나 읽을 수 없으면, 결과의 `after`·`files`에 해당 경로의 오류를 남기고 `needs-inspection`으로 보존한다. 정상 파일의 SHA-256과 없는 파일의 `null` 형식은 유지한다. 오류가 있는 결과는 성공으로 수용하지 않으며 예약도 자동 해제하지 않는다. native 역할 도구가 끝난 것을 확인해 `native-result`로 수집한 뒤, 현재 변경과 오류를 확인하고 아래 `reconcile` 절차로 닫는다.

`native`가 false면 별도 supervisor가 `dispatch` 종료 뒤에도 작업을 이어간다. 다음 명령은 로컬 상태만 확인하며 모델을 반복 호출하지 않는다. 작업 전 rate limit으로 native 세션에 handoff될 수 있으므로 반환된 레코드의 `native`를 매번 확인한다. true가 되면 `actualRole`·`brief`를 native 도구로 실행하고 결과를 기록한다. 완료된 결과와 근거를 확인한 뒤 작업에 반영한다.

```text
chef worker wait --key "<key>" --seconds 30
```

`status --key <key>`는 즉시 반환하는 로컬 조회다. 새 소유자 세션으로 미션을 재개했으면 ACTIVE 소유자를 먼저 확인한 뒤 `recover-owner`로 기존 작업 결과의 접근 소유권을 바꾼다. 이 명령은 작업을 다시 실행하지 않는다. 이전 소유자에 연결된 실행 중 워커는 소유권 검사에서 멈춘다.

```text
chef worker recover-owner --key "<key>" --token "<token>" --runtime "codex" --session "resumed-session-id"
```

취소가 필요할 때 반환된 시도 token으로 요청한다. 워커가 멈춘 뒤 부분 변경을 확인한다. native 작업은 역할 도구를 취소하고 결과를 수집한 뒤 취소 결과를 기록한다.

```text
chef worker cancel --key "<key>" --token "<token>"
```

중단된 워커가 프로세스나 부분 수정을 남길 수 있다. 추적 중인 프로세스가 모두 끝나고 출력 변경을 확인할 때까지 재시도하거나 파일 범위를 풀지 않는다. 이전 작업을 정리하려면 프로젝트 상대 근거 경로를 지정한다. `reconcile`은 관찰한 해시와 현재 출력 파일 상태를 기록하고 기존 시도를 실패로 닫지만 수정 내용을 되돌리지는 않는다. 후속 시도는 새 `requestId`를 쓴다.

```text
chef worker reconcile --key "<key>" --token "<token>" --evidence "reports/inspection.md"
```

상태 저장소 기본 경로는 `~/.chef/workers`다. `CHEF_WORKERS_HOME` 또는 명령 앞에 쓰는 전역 `--home` 옵션으로 바꿀 수 있다. 이 저장소는 같은 컴퓨터의 Chef 세션을 조정하는 로컬 기록소이며 원격 큐가 아니다. 자체 프로세스는 POSIX process group 또는 Windows Job Object로 관리한다. POSIX에서 해당 그룹을 벗어난 프로세스는 종료 보장에 포함되지 않는다. 도구·파일 범위 검사는 협력형 가드이며 일반 OS 파일시스템 sandbox가 아니다. 결과를 수용하기 전에 diff와 근거를 확인한다. `needs-inspection`은 부분 변경을 reconcile한 뒤에만 재시도한다. 라우터는 계정 할당량·잔여 크레딧·과금을 추산하지 않는다. 외부 워커는 각 런타임의 로그인이 필요하며 서비스 가용성이나 계정 정책에 따라 실패할 수 있다.

## 가드 오류와 작업 수명

필수 명령이 거부되면 다른 셸·래퍼로 반복하지 않고 명령·이유·변경 파일·미실행 검증을 반환한다. 외부 워커는 반복된 같은 거부를 `blocked.json`에 보존하고 실행을 중단한다. 이 결과는 성공이나 사용자 취소로 처리하지 않는다. 읽기 역할의 셸은 알려진 조회 명령에만 한정되며 임의 인터프리터·명령 실행 옵션·리디렉션·쓰기 파이프는 허용하지 않는다.

입력 보호와 접수 잠금은 실제 워크스페이스 identity로 분리한다. 다른 프로젝트의 파일 스캔을 기다리지 않는다. 입력 링크 인덱스를 준비하는 동안 해당 워크스페이스의 쓰기만 잠시 보호한다. 같은 실제 파일의 충돌 검사와 실행 후 해시 검사는 유지한다. 업데이트할 때 이전 버전의 실행을 먼저 수집하고 설치본·훅을 함께 갱신한다. dashboard 표시 코드만 바뀐 경우에는 역할 워커 권한을 무효화하지 않는다.
