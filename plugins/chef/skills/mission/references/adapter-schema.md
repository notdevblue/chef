# 어댑터 계약

프로젝트마다 다른 값은 어댑터 두 파일에 둔다.

- `adapter.json` — 훅과 스킬이 기계적으로 읽는 값
- `adapter.md` — 사람과 모델이 읽는 절차(작업 단위 만들기, 검증 순서, 함정)

## OS와 런타임 병합

`platforms.windows`·`platforms.macos`·`platforms.linux`는 해당 OS의 덮어쓰기다. `runtimes.claude`·`runtimes.codex`·`runtimes.grok`·`runtimes.hermes`를 모두 지원한다.
각 파일의 병합 순서는 공통 → 해당 OS → 해당 런타임 → 해당 런타임 안의 해당 OS다. `adapter.json`의 이 순서를 마친 뒤 `local.json`에도 같은 순서를 적용해 마지막에 병합한다. 객체는 깊게 병합하고 배열·스칼라는 교체한다. 따라서 공통 파일의 런타임·OS 값이 개인 `local.json`을 이기지 않는다. 가드·미션·doctor·슬롯은 같은 로더를 사용한다. OS 이름·선택된 overlay 형식이 잘못되면 오류로 처리한다.

```json
{
  "platforms": {"linux": {"slots": {"terminal": "tmux"}}},
  "runtimes": {
    "claude": {"slots": {"beforeRun": []}},
    "codex": {"platforms": {"macos": {"slots": {"terminal": "macos-terminal"}}}}
  }
}
```

Claude 설정·메모리 준비 단계는 `runtimes.claude.slots.beforeRun`에만 둔다. 공통 단계에 두면 다른 런타임에도 상속되므로 주의한다. 배열을 합쳐 붙이지 않으며 필요한 전체 단계 배열을 지정한다.

## 어댑터 찾기

미션 시작·재개는 [공통 진입 CLI](mission-entry.md)를 먼저 실행한다. 선택적으로 등록한 외부 이벤트는 어댑터가 없어도 준비를 실행할 수 있다.
이벤트가 적용되는 root의 open·resume은 어댑터 탐색보다 먼저 진입 receipt를 확인한다. 아래의 어댑터 없음 알림은 진입 등록이 없는 환경에 해당한다.

### Claude

스킬과 가드 훅은 같은 순서로 찾는다. 시작 폴더는 `CLAUDE_PROJECT_DIR`, 없으면 cwd.

1. **저장소 안 어댑터** — 시작 폴더에서 위로 올라가며 `.claude/orchestrator/adapter.json`이 있는 첫 폴더.
   팀과 같이 쓰는 프로젝트에서 어댑터를 저장소에 올릴 때 쓴다.
2. **개인 레지스트리** — 위가 없으면 레지스트리 폴더의 `projects.json`의 `roots`와 시작 폴더·그 상위를
   대조한다. 맞으면 `<레지스트리>/<adapter>/adapter.json`을 쓰고, 맞은 폴더가 프로젝트 폴더다. 어댑터를 VCS에
   올리지 않고 개인적으로만 가질 때 쓴다. 레지스트리 폴더는 다음 순서로 정한다.
   - 환경변수 `HAN_ADAPTERS_DIR`. 스킬·스크립트(Bash 도구)가 읽는 값이다. SessionStart 훅이 플러그인 설정
     `adapters_dir` 값을 `CLAUDE_ENV_FILE`에 `export`로 적어 넣어 주므로, 설치본에서는 설정만 해 두면 된다
   - 플러그인 설정 `adapters_dir`. 설치 때 입력하고 훅에는 `CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR`로 들어간다
     (Bash 도구 환경에는 없다)
   - 플러그인을 저장소에서 바로 돌릴 때의 `<저장소>/adapters`. 설치본은 플러그인 캐시에서 돌아 이 폴백이 없다

```json
[
  { "adapter": "example", "roots": ["<absolute-project-path>", "<absolute-slot-parent>/*"] }
]
```

`roots`는 경로나 glob이다. Windows 형식 경로는 구분자·대소문자를 정규화하며, POSIX 경로는 파일시스템의 경로 식별을 보존한다. 둘 다 없으면 어댑터 없음이다.
그때 스킬은 멈춘다. 미션 전환 명령(`mission_control.py open|resume|close <id> ...`)은
어댑터를 찾지 못하면 거부하고 상태를 보존한다. 다른 명령에는 기존 어댑터 검색 동작을 유지한다.
이유는 레지스트리 경로 미설정, 레지스트리 폴더 없음, `projects.json` 없음, 맞는 `roots` 없음 중 하나다.

어댑터를 찾으면 같은 폴더의 `local.json`(PC별 값, VCS 제외)을 깊게 병합해 읽는다. 객체는 키별로 합치고
배열·스칼라는 `local.json` 값이 통째로 덮는다. `schema`는 병합 뒤 값으로 검사한다.

### Codex

Codex는 훅 payload의 cwd를 시작점으로 삼는다. 명령에 cwd가 있으면 그 폴더를 사용한다.
`CLAUDE_PROJECT_DIR`와 Claude 플러그인 옵션은 Codex에서 읽지 않는다.

1. 시작 폴더에서 위로 올라가며 `.codex/orchestrator/adapter.json`을 확인하고,
   같은 폴더에 없으면 `.claude/orchestrator/adapter.json`도 확인한다. 가까운 폴더가 우선이다.
2. 없으면 시작 셸의 `HAN_ADAPTERS_DIR`을 사용한다. 값이 없거나 비어 있으면
   `<CODEX_HOME>/config.toml` 또는 `~/.codex/config.toml`의 기존
   `[shell_environment_policy.set].HAN_ADAPTERS_DIR`을 읽는다. 환경변수는 설정값보다 우선한다.
3. 둘 다 설정되지 않았고 저장소에서 직접 실행할 때만 `<저장소>/adapters`를 사용한다.
   설치 캐시에는 이 폴더가 없으므로 개인 레지스트리 경로를 환경변수나 Codex 설정에 둔다.
   TOML 폴백은 표준 라이브러리 `tomllib`을 쓰므로 Python 3.11 이상이 필요하다.

Claude와 Codex는 같은 레지스트리·stateDir·슬롯 잠금 파일을 공유한다.
Codex 덮어쓰기는 `runtimes.codex` 객체에 둔다. 위 OS·런타임 병합 순서를 적용하며 공통 PC별 경로는 `local.json`에서 관리한다.
스킬과 가드, check_guard, slots가 이 병합 규칙을 같이 쓴다.

`AdapterNotFound`가 나면 미션 전환 명령(`mission_control.py open|resume|close <id> ...`)을
거부하고 상태를 보존한다. 다른 명령에는 기존 동작을 유지한다. 가능한 이유는
시작 폴더 없음, 경로 미설정, 레지스트리 폴더나 `projects.json` 없음, 맞는 `roots` 없음이다.
Codex 설정 읽기·형식 오류 또는 Python 버전 부족은 `GuardOff`다. 전환 명령은 이를 거부하고,
일반 명령은 기존 가드 진단을 유지한다. 미션을 열거나 재개한 뒤에는 CLI 실행 성공과
`<stateDir>/ACTIVE`의 미션 id·현재 세션 id·runtime이 모두 일치하는지 확인한다.

## Grok에서 상태 읽기

Grok 코디네이터는 상태를 읽고 메인 또는 등록된 슬롯의 실행자에게 작업을 인계할 수 있다. 실제 봇 이름과 실행 도구는 개인 환경에서 연결한다.
Claude(`/chef:mission`)와 Codex(`$chef:chef-mission`)는 선택 경로다.
조회 스크립트 `plugins/chef/grok/scripts/han_state.py`의 `snapshot`은 `ACTIVE`·`mission.md`·`slots.tsv`를 읽기만 한다.
`brief --runtime grok`는 SendToAgent 인계 블록을 출력하고, `claude`·`codex`는 붙여 넣을 문장을 출력한다. 조회 명령은 미션 파일을 쓰지 않는다.
Grok SessionStart는 `new`·`load`를 지원한다. 미션 가드는 Grok 도구 이벤트와 도구 이름을 공통 가드 입력으로 정규화하며, `open`·`resume`·`close` 마커를 처리한다. `open`·`resume` 뒤에는 `ACTIVE`가 미션 id와 현재 세션 id를 가리키는지 확인하고, 확인된 뒤 `init`을 실행한다. 공유 transcript reader는 ACP `updates.jsonl` 형식을 읽으며, 경로는 훅 컨텍스트나 세션 기록에서 확인한 값을 사용한다.
Grok CLI·Bot에 등록하는 스킬은 `chef-status`, `chef-doctor`, `chef-mission-brief`, `chef-run`, `chef-mission`, `chef-slots`, `chef-adapter-init`, `chef-distill`이다.
`chef-watch`와 `chef-handoff`는 그 등록 스킬을 가리키는 저장소 안내이며 등록하지 않는다.

Grok의 검색은 아래 순서를 쓴다. Claude·Codex의 검색 순서는 그대로다.
가까운 폴더가 우선이고, 한 폴더 안에서는 앞에 있는 파일이 우선이다.

1. `.grok/orchestrator/adapter.json`, 이어서 같은 폴더의 `.claude/orchestrator/adapter.json`, 이어서 `.codex/orchestrator/adapter.json`.
2. 없으면 환경변수 `HAN_ADAPTERS_DIR`의 `projects.json`.
3. 없으면 `HAN_REPO/adapters`. `HAN_REPO`는 이 저장소를 받은 폴더다. 폴더를 지정했는데 `adapters`가 없으면 조회가 오류로 끝난다.
4. 그것도 없으면 이 저장소에서 스크립트를 직접 실행할 때 `<저장소>/adapters`.

Claude 플러그인 설정과 Codex `config.toml`은 읽지 않는다. Grok은 마켓플레이스에서 `chef@chef`를 설치하고, 선택 경로는 `grok inspect`로 확인한다.
Grok 전용 값은 `runtimes.grok` 객체에 두며 위 OS·런타임 병합을 쓴다. 조회·미션·가드·doctor·슬롯이 같은 병합을 사용한다.

역할을 명시할 때는 아래처럼 native 이름을 쓴다. 기본 역할의 호환 이름과 파견 규칙은 [Grok 역할 계약](../../../grok/skills/chef-mission/references/grok-roles.md)에 있다.

```json
{
  "runtimes": {
    "grok": {
      "roles": {
        "research": "chef:researcher",
        "implement": "chef:dev-medium",
        "verify": "chef:verifier",
        "review": "chef:code-reviewer",
        "audit": "chef:code-reviewer"
      }
    }
  }
}
```

## adapter.json

```json
{
  "schema": 1,
  "project": "Game",
  "paths": {
    "vcsRoot": ".",
    "stateDir": "_My/missions"
  },
  "stages": [
    { "id": "content", "kind": "research", "label": "컨텐츠 확인" },
    { "id": "poc", "kind": "design", "label": "PoC", "skill": "game-poc", "optional": true },
    { "id": "impl", "kind": "implement", "label": "구현 진행" },
    { "id": "review", "kind": "review", "label": "검토 & 리뷰" }
  ],
  "guard": {
    "deny": [
      { "pattern": "^(?:&\\s*)?[\"']?(?:\\S*[\\\\/])?p4(?:\\.exe)?[\"']?(?:\\s+-\\w+(?:\\s+[^-\\s]\\S*)?)*\\s+submit\\b",
        "reason": "미션 중 submit 금지 — 확인 C에서 사용자가 요청할 때만" }
    ]
  },
  "roles": {
    "research": "chef:researcher",
    "implement": "chef:dev-medium",
    "verify": "chef:verifier",
    "review": "code-reviewer",
    "reviewSmall": "chef:reviewer-small",
    "reviewLoopSkill": "review-loop"
  },
  "rolePolicy": { "model": "inherit", "subagents": false },
  "verify": {
    "backend": "mcp",
    "grades": ["editmode-pass", "pytest-pass", "compile-only", "doc-only"]
  },
  "report": {
    "push": true
  },
  "slots": {
    "enabled": true,
    "names": ["slot1", "slot2", "slot3"],
    "vcs": "perforce",
    "client": "ws-yourname_{name}",
    "stream": "//Depot/Main",
    "mainClient": "ws-yourname_Main",
    "prepare": [
      { "id": "sync", "group": 1, "run": "p4 -c {client} sync -q --parallel=threads=8 //Depot/Main/...@{baseline}" },
      { "id": "setup", "group": 2, "argv": ["{python}", "{slot}/tools/project.py", "setup", "--root", "{slot}"] },
      { "id": "verify", "group": 3, "cwd": "{slot}", "argv": ["{python}", "{slot}/tools/project.py", "verify", "--root", "{slot}"], "okExit": [0] }
    ],
    "refresh": ["sync", "setup", "verify"],
    "editorArgv": ["{editorExe}", "--project", "{slot}"]
  }
}
```

예시에 나오는 `VcsWrapper`·`QualityGate`·`ProjectOps`·`project-build.bat`는 가상의 프로젝트 도구 이름이다. 실제 프로젝트의 명령으로 바꿔 쓴다.

PC마다 다른 값은 같은 폴더의 `local.json`에 둔다. 깊은 병합으로 `adapter.json` 위에 얹히고 VCS에 올리지 않는다.
레지스트리에는 사람마다 고칠 자리를 보이는 `local.example.json`을 두고, 복사해서 `local.json`으로 쓴다.

```json
{
  "slots": {
    "root": "<absolute-slot-parent>",
    "client": "ws-yourname_{name}",
    "mainClient": "ws-yourname_Main"
  },
  "vars": {
    "editorExe": "<absolute-editor-executable>",
    "main": "<absolute-project-path>"
  }
}
```

| 키 | 뜻 |
| --- | --- |
| `schema` | 계약 버전. 코어가 모르는 값이면 미션을 시작하지 않는다 |
| `project` | 보고서·상태 파일에 쓰는 이름 |
| `paths.vcsRoot` | 프로젝트 폴더 기준 VCS 루트 상대 경로. 프로젝트 폴더 = VCS 루트라고 가정하지 않는다 |
| `paths.stateDir` | 프로젝트 폴더 기준 미션 상태 폴더. VCS에 올리지 않는 곳이어야 한다 |
| `stages[]` | 미션 단계 순서. `kind`는 `research` / `design` / `implement` / `review`. `design`은 `skill`과 `optional`을 가진다. 없으면 `research → implement → review` |
| `guard.deny[]` | 미션 진행 중 막을 명령의 정규식(대소문자 무시)과 이유. 명령 조각마다 검사한다(아래 "가드") |
| `guard.deny[].except` | 선택 문자열 배열. 각 정규식이 같은 조각 전체에 맞을 때 그 deny 규칙만 면제한다. 다른 조각·규칙은 계속 검사한다 |
| `guard.deny[].exceptCommand` | 선택 정규식 배열. 원문 전체가 맞고 가드가 단일 리터럴 조회·출력 명령으로 인정할 때 그 deny 규칙만 면제한다. 연쇄·파이프·치환·동적 실행·제어 문자·리다이렉션은 예외 불가. 기존 `except`의 조각 fullmatch 동작은 유지 |
| `guard.allow[]` | 선택 문자열 정규식 배열. 명령 조각 **전체**가 맞으면(`fullmatch`, 대소문자 무시) 그 조각은 `deny`·`p4Deny`를 보지 않는다. 여러 규칙이 같이 쓰는 검증된 예외(예: `--check-only`)를 한 곳에 둔다. `p4Client` 검사는 그대로 받는다 |
| `guard.p4Deny[]` | 선택. p4 하위 명령 이름으로 막는 규칙 `{"commands": [...], "when": "<정규식>", "unless": "<정규식>", "reason"}`. 실행 파일 경로·따옴표·`&`·전역 옵션(`-c`·`-u`·`-p` 등)은 훅이 해석하므로 정규식에 다시 쓰지 않는다. `when`·`unless`는 하위 명령 뒤 인자(따옴표 벗겨 공백으로 이음)에 `search` |
| `guard.coordinatorWrite` | 선택 glob 배열(프로젝트 폴더 기준, `**` 여러 단계·`*` 한 단계, 대소문자 무시). 있으면 미션 진행 중 ACTIVE 세션의 메인 코디네이터(`agent_id` 없음)가 Write·Edit·MultiEdit·NotebookEdit로 프로젝트 안 이 목록 밖 파일을 쓰면 막는다. 서브에이전트·다른 세션·프로젝트 밖 경로(메모리 등)는 보지 않는다. 예: `["_My/missions/**"]`. Claude 훅만 적용(Codex 훅은 셸 명령만 받음) |
| `guard.p4Client` | 선택. `{"require": true}`면 미션 중 p4 쓰기 명령에 전역 `-c <작업 공간>` 필수, 이 폴더 작업 공간(슬롯 `slots.client` 틀·메인 `slots.mainClient`·`.p4config`)과 달라도 막음. `commands`로 대상 하위 명령 목록을 바꿀 수 있다. 래퍼 `p4c.py`가 자동으로 붙인다 |
| `mission.staleHours` | 선택. ACTIVE 끊김 의심 기준 시간(기본 24, 0이면 끔). 환경변수 `HAN_STALE_HOURS`가 우선. 표시만 하고 자동으로 닫지 않는다(`close-stale --force-close-stale`) |
| `roles.*` | 역할별 실제 에이전트·스킬 이름. 없는 역할의 기본값은 chef 역할이다(research `chef:researcher`, implement `chef:dev-medium`, verify `chef:verifier`, review·audit `chef:code-reviewer`, reviewSmall `chef:reviewer-small`). 일반 에이전트로 대신하지 않고, 역할 에이전트가 없으면 사용자에게 알린다 |
| `roles.implementSmall` | 승인된 문서·정해진 소규모 수정 담당. 없으면 implement. 구현과 독립 리뷰 분리는 유지 |
| `roles.verify` | 테스트·Unity Play 실행 전담. 기본 역할·모델·effort는 런타임 진입점의 역할 계약. 코드 수정·모델 전환·완료 판정 금지 |
| `roles.review` | 첫 심층 리뷰를 맡을 읽기 전용 역할. 프로젝트 리뷰어와 기본·작은 리뷰 라우팅·모델은 런타임 역할 계약. 작은 문서 첫 리뷰의 뜻·승인 범위 조건은 공통 mission 절차 |
| `roles.reviewSmall` | 직전 리뷰 이후의 좁은 재리뷰와 뜻·승인 범위가 그대로인 작은 문서 변경의 첫 리뷰를 맡을 읽기 전용 역할. 없으면 `chef:reviewer-small`. 프로젝트 리뷰 규약이 `roles.review`만 읽게 정하면 그 규약이 우선 |
| `roles.audit` | 미션 끝 기록 감사(SKILL §6의 4)를 맡을 읽기 전용 에이전트. 없으면 `roles.review`. 구현 역할과 다른 모델이면 더 좋다 |
| `roles.reviewLoopSkill` | 선택. 미션 전체 리뷰에 사용할 프로젝트 스킬 이름. 에이전트 역할이 아니므로 `agents.allow`의 파견 이름에 포함하지 않는다. 해당 스킬을 읽고 현재 런타임의 역할·리뷰 종료 계약을 적용한다. 없으면 공통 mission 절차의 전체 리뷰를 따른다 |
| `rolePolicy` | 역할 파견 정책 `{"model":"definition" 또는 "inherit","subagents":true 또는 false}`. 기본은 `definition`·`true`. 모든 런타임과 OS overlay에서 같은 병합을 사용한다. 역할 이름·정의 파일의 모델과 스킬 밖 직접 호출은 바꾸지 않는다 |
| `rolePolicy.model` | `definition`은 스킬 문서의 모델 배정을 따른다. `inherit`는 Claude에서 Agent 호출 `model` 인자에 코디네이터 모델의 별칭(`sonnet`·`opus`·`haiku`·`fable`)을 넘기고(인자는 별칭만 받으며, 대응하는 별칭이 없으면 정의 모델로 두고 보고), Codex는 등록된 역할의 TOML 모델이 적용되어 부모 상속을 지킬 수 없으므로 그 사실을 보고하며, Grok은 역할이 이미 `model: inherit`라 추가 동작이 없다. effort 지시는 바꾸지 않는다 |
| `rolePolicy.subagents` | `false`면 하위 파견을 허용하던 역할(Claude `chef:dev-medium`, Codex `chef:dev-medium`)의 파견 브리프에 "하위 에이전트를 띄우지 않는다. 나눠야 하면 단위·파일 경계·계약을 담은 분할안을 부모에게 반환한다"를 넣는다. 해당 역할 정의가 브리프의 금지 지시를 따르게 하는 것이며 도구로 막는 강제 차단은 아니다. Grok 역할은 어차피 자식을 띄우지 않는다 |
| `agents.allow` | 파견할 역할 이름의 glob 배열. 없으면 제한 없음. `runtimes.<runtime>.agents` 및 OS overlay로 덮어쓸 수 있으며 배열은 통째로 교체한다 |
| `workers.mode` | 선택. Claude·Codex·Grok 공통 파견 모드. `auto`(기본)는 자동 배정, `native`는 현재 서비스만, `claude`·`codex`·`grok`은 지정 서비스만 쓴다. 단일 서비스 모드는 다른 서비스 인증과 quota fallback도 하지 않는다. CLI `--mode` > 요청 `mode` > 환경변수 `CHEF_WORKERS_MODE` > 이 값 > 전역 `~/.chef/config.json`의 `workers.mode` > `auto` 순이다. Hermes 기존 backend 선택은 바꾸지 않는다. 상세는 [공통 워커 계약](cross-runtime-workers.md#자동-배정-켜기끄기)을 따른다. |
| `workers.runtimes` | 선택. 공통 워커에 허용할 `claude`·`codex`·`grok`의 비어 있지 않은 중복 없는 배열. 생략하면 전역 설정, 전역에도 없으면 세 서비스 모두 허용한다. 요청 > 프로젝트 > 전역 순이며 목록 전체를 대체한다. 제외한 서비스는 인증과 quota 대체에서도 제외한다. 현재 코디네이터의 서비스는 바꾸지 않는다. |
| `workers.roleRouting.<canonicalRole>.runtime` | 선택. 정규 Chef 역할의 서비스를 `claude`·`codex`·`grok`으로 고정한다. `auto`는 상속한 해당 역할의 고정을 푼다. 정규 역할 여섯 개를 사용하며 전역 → 프로젝트 → 요청 순으로 역할별 항목을 덮어쓴다. 고정이 `runtimes`나 단일 서비스 모드와 충돌하면 파견 전에 오류다. 모델·effort는 기존 역할 설정을 따른다. [허용 서비스·역할 고정](cross-runtime-workers.md#허용-서비스와-역할별-고정) |
| `workers.roleDefinitions.<runtime>.<role>` | 선택. 공통 Claude·Codex·Grok 워커가 사용할 사용자 정의 역할 정의의 절대 경로. `<runtime>`은 `claude`, `codex`, `grok`; `<role>`은 `roles`에 지정한 역할 이름이며 파일은 일반 파일이고 심볼릭 링크면 안 된다. Claude·Grok은 frontmatter가 있는 Markdown, Codex는 TOML을 읽는다. 기본 Chef 역할 정의를 대체할 때 그 역할 책임·도구·금지 범위를 해당 런타임 파일에 적는다. |
| `workers.<runtime>Roles.<canonicalRole>` | 선택. 공통 워커 라우터에서 해당 런타임·정규 Chef 역할의 `model`·`effort`만 덮어쓴다. `<canonicalRole>`은 `researcher`, `dev-small`, `dev-medium`, `verifier`, `code-reviewer`, `reviewer-small`. 런타임 자체를 지정하지 않으며 라우터는 계속 가용성·도구·작업 조건으로 선택한다. 이 키는 Hermes 경로의 역할 설정과도 공유되므로 그 경로의 적용도 함께 고려한다. |
| `workers.codexArguments` | 선택. Hermes `chef_delegate`가 띄우는 Codex 역할 워커(사전 probe 포함)에 덧붙일 `"-c"`, `"key=value"` 쌍의 문자열 배열. sandbox 세부 설정·셸 환경 같은 프로젝트 설정에 쓴다. 계정·모델·지침·sandbox 모드·hook 기능·승인 정책·훅 키(`model_provider(s)`, `forced_login_method`, `developer_instructions`, `model`, `model_reasoning_effort`, `sandbox_mode`, `profile(s)`, `features`, `approval_policy`, `hooks`, `mcp_servers`, `sandbox_workspace_write.writable_roots`)는 바꿀 수 없고, 형식이 틀리면 인증·실행 전에 거부한다. 세션 플래그 훅은 저장된 hook trust가 없는 무인 워커에서 실행되지 않으므로 프로젝트 검사는 `workers.guardCommand`를 쓴다. `runtimes.hermes.workers`와 OS overlay를 지원한다 |
| `workers.hermesRoles` | 선택. Hermes 기본 backend의 역할별 `model`·`provider` 문자열 라우팅 지정. 지정하지 않은 값은 활성 Hermes 프로필 설정을 사용한다. 역할 키는 `researcher`, `dev-small`, `dev-medium`, `verifier`, `code-reviewer`, `reviewer-small` 또는 `chef:` 접두 이름이다 |
| `workers.readOnlySources` | 선택. Hermes native 워커에게 슬롯 밖 읽기를 허용할 정규 절대 파일 경로 → SHA-256 사전. 정확한 파일만 허용하며 디렉터리·링크·외부 쓰기는 허용하지 않는다. 준비 검사와 매 읽기에서 해시를 확인한다 |
| `workers.roleContracts` | 선택. Chef 역할별 `{path, sha256, modelPolicy}`. 슬롯 상대 TOML의 지침과 `definition`·`inherit` 정책을 연결한다. native 실행은 지침 본문을 주입하고 고정 model을 실제 실행에 적용·대조한다. Hermes 워커별 effort는 두지 않으며 TOML의 `model_reasoning_effort`는 가져오지 않는다. reasoning 설정은 Hermes 호스트를 따른다 |
| `workers.claudeRoles` | 선택. Hermes `claude` backend의 역할별 `model`·`effort` 지정. effort는 `low`·`medium`·`high`·`max`. 기본값은 설치된 Chef 역할 정의를 따른다. 로그인된 Claude CLI와 설치된 Chef hook 확인이 필요하다 |
| `workers.claudeSettings` | 선택. Hermes `claude` backend용 인증 설정 JSON 파일의 절대 경로. 전체 사용자·프로젝트 설정을 불러오지 않는다. `env`, `apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `gcpAuthRefresh`, `forceLoginMethod`, `forceLoginOrgUUID`, `modelOverrides`만 허용한다. 개인 `local.json`에 경로를 두고 대상 JSON은 저장소 밖에 비공개로 보관한다 |
| `workers.attachments.<이름>` | 선택. Hermes의 명명된 외부 worker backend 설정. backend 이름은 소문자 영숫자 또는 하이픈으로 시작하는 형식이며 항목은 `{"command":["/절대/실행파일","인자"]}`만 받는다. 실행 파일은 schema 1 JSONL `chef-tools-v1` 프로토콜을 구현해야 한다. 도구 요청은 같은 native 작업 가드를 통과하지만 bridge 실행 파일과 자식 프로세스는 신뢰 경계이며 OS sandbox가 아니다 |
| `workers.guardCommand` | 선택. Hermes의 Codex 역할 워커 도구 호출마다 Chef의 워커 PreToolUse 훅이 실행할 프로젝트 검사 argv 배열. 표준 입력으로 정규화한 훅 payload JSON(`session_id`=소유 Hermes 세션, `agent_id`=`chef-codex-worker`, `han_runtime`=`hermes`)을 받는다. 출력이 없거나 `permissionDecision: allow`면 허용하고, deny·block 결정, 그 밖의 출력, 0이 아닌 종료, 실행 실패, 8초 초과는 거부한다. 명령은 위임 시작 시 워커 문맥에 고정한다. 실행 파일이 없으면 위임을 인증 전에 거부한다. Claude 역할 워커는 별도의 Chef hook을 사용한다 |
| `verify.backend` | `mcp` / `cli` / `dotnet` — 엔진 검증 수단. 명령 자체는 `adapter.md` |
| `verify.grades` | 이 프로젝트에서 낼 수 있는 검증 등급. 실제 허용 값은 상태 CLI `schema`로 조회 |
| `verify.resourceDir` | 공유 검증 잠금 폴더. 절대 경로 또는 프로젝트 기준 상대 경로. `resources`에 자원을 선언하면 필수이며 같은 자원을 쓰는 프로젝트·슬롯은 같은 폴더 사용 |
| `verify.resources` | 선택 등급별 자원 이름 배열. 예: `{"compile-only": ["preview-editor"]}`. 이름은 영숫자로 시작하는 1~64자의 영숫자·`_`·`-`; 중복·대소문자 충돌 금지. 등급 키는 정확히 일치하며 wildcard 없음 |
| `verify.readiness` | 선택 등급별 준비 확인 명령 배열. 예: `{"compile-only": [{"argv": ["python", "tools/check_editor.py", "--project", "{project}"], "cwd": ".", "timeoutSeconds": 30}]}`. timeout 1~60초, argv의 `{project}`만 치환. 자원 잠금 뒤 receipt 재사용 전 실행. 읽기 전용 preflight는 명령을 실행하지 않음 |
| `verify.coordinatorChecks` | 선택적 코디네이터 실행 허용 목록. 항목은 `{"grade":"doc-only","commands":[{"argv":["python","tools/check_docs.py"],"cwd":".","tools":{"tools/check_docs.py":"<소문자 sha256>","<python 실행 파일 경로>":"<소문자 sha256>"}}]}`. 한 항목의 등급·명령 순서·argv·해석된 cwd가 계획 전체와 정확히 일치해야 한다. 도구·런처와 추가 의존 파일의 현재 SHA-256을 모두 명시한다. 계획의 모든 명령은 `reusable`을 명시하고 범위가 좁은 비어 있지 않은 `inputs`, 빈 `outputs`를 가져야 한다. readiness·resources·결과 파일 검사, 루트 전체 입력·출력, 지원 목록에 없는 실행기와 보수적으로 분류되는 interpreter 래퍼 계획은 허용하지 않는다. 모든 shell 계열 실행기는 verifier 경로에 남으며 `.bat`·`.cmd`도 허용하지 않는다. 설정과 pinned dependency hash는 receipt identity에 포함되므로 정책·도구 변경은 기존 결과 재사용을 막는다. 설정이 없으면 기존 receipt identity와 verifier 경로를 유지한다. WindowsApps alias처럼 실제 파일 내용을 읽어 해시를 구할 수 없는 실행기는 직접 허용되지 않는다. 설정은 명령이 읽기 전용·결정론적인지 증명하지 않으므로 운영자가 확인해야 한다. 설정이 잘못된 경우 일반 verifier 경로는 호환되고, `--coordinator` 경로는 거부한다 |
| 리뷰 전 기계 검사 | 새 필드를 두지 않는다. `adapter.md`가 정한 lint·규약 검사 명령을 검증 계획의 `commands[]`에 넣어 리뷰 파견 전에 통과시키고 그 출력을 리뷰어에게 준다 |
| `report.push` | 마일스톤마다 푸시 알림을 보낼지 |
| `slots.autocompact` | 슬롯 세션 압축 예산(100000~1000000 토큰, 기본 Claude 500000, Codex 200000). Claude와 Codex 실행 옵션에 적용 |
| `slots.*` | 에이전트 슬롯 워크스페이스(아래 "슬롯"). 없거나 `enabled: false`면 슬롯을 안 쓴다 |
| `vars` | 준비 단계·에디터 명령 템플릿에서 `{키}`로 쓰는 PC별 값(아래 "슬롯"의 템플릿 변수). `local.json`에 둔다 |
| `local.json` | 같은 폴더의 PC별 덮어쓰기. 깊은 병합(객체는 키별, 배열·스칼라는 통째로 덮음). git에 올리지 않는다 |

`slots.root`·`slots.client`·`slots.mainClient`·`vars`처럼 PC·사람마다 다른 값은 `adapter.json`이 아니라 `local.json`에 둔다. 저장소 안 어댑터에는 `slots`를 두지 않는다.

## 가드

가드 훅은 Bash·PowerShell 명령마다 돈다(`guard.coordinatorWrite`가 있으면 Claude의 파일 쓰기 도구에도). 어댑터를 못 찾으면 미션 마커 명령에만 `가드 꺼짐` 알림을 주고
(위 "어댑터 찾기"), 다른 명령에는 아무것도 하지 않는다.

- **조각 검사**: 줄 이음(Bash `\`, PowerShell `` ` `` + 줄바꿈)을 합치고 줄바꿈·`;`·`&&`·`||`·`|`·`&`·`{`·`}`로
  나눈다. `cmd /c`·`xargs`·`env` 같은 래퍼를 벗기고, `$(...)`·백틱 안쪽도 조각으로 본다. 같은 명령 안의
  단순 변수 대입(`NAME=값`, `$NAME = 값`)은 뒤 조각에서 치환한다. 규칙은 `^`로 조각 첫머리에 고정해
  "실행 파일 바로 뒤 동사"를 보게 쓴다. 파일 경로 속 단어(`Foo.Sync.cs`)를 동사로 오인하지 않게 한다.
- **대소문자**: deny 규칙 객체의 `caseSensitive`는 boolean이며 기본 `false`다. `true`이면 경로·리터럴을 대소문자 그대로 검사한다. `except`·`exceptCommand`도 부모 규칙의 flag를 상속한다. 잘못된 타입은 진단·오류 처리한다.
- **규칙 안 예외**: `except`는 문자열 정규식 배열이며 부모 규칙의 대소문자 설정을 따르는 `fullmatch`로 같은 조각 전체를 검사한다.
  전역 허용이 아니므로 조회 뒤의 `; p4 submit`과 하위 실행 조각, 다른 deny 규칙은 계속 검사한다.
  배열 타입·항목 타입·정규식 중 하나라도 잘못되면 진단을 내고 그 규칙의 예외 전체를 적용하지 않는다.
  예: `{"pattern": "submit", "except": ["rg -n submit Tool"], "reason": "submit 실행 금지"}`는 그 조회 조각만 면제한다.
  정규식은 실행 파일·옵션·인수를 좁게 열거한다. `.*--check-only.*`나 `rg.*`처럼 실행·표현식을 삼킬 수 있는 예외는 두지 않는다.
  예제 어댑터는 제한된 옵션의 `rg` 리터럴 조회(경로는 `/`), 리터럴 `Write-Output`,
  `Tool/ProjectOps/python-runner.bat Tool/ProjectOps/cli.py submit <양수번호CL> --check-only`만 면제한다.
  프로젝트에 적용할 때는 해당 도구의 `--check-only`가 shelve·잠금 변경·CI·submit 전에 반환하는지 먼저 확인한다.
  임의 실행 파일의 `--check-only`와 `--dry-run`은 면제하지 않는다.
- **전역 허용·p4 규칙**: `guard.allow`는 여러 deny 규칙에 같은 예외를 반복하지 않게 하는 조각 단위 허용 목록이다.
  `except`처럼 조각 전체 일치만 보며, 같은 명령의 다른 조각은 계속 검사한다. p4 쓰기 규칙은 긴 접두 정규식 대신
  `guard.p4Deny`로 하위 명령 이름을 적는다. 예: `{"commands": ["shelve"], "when": "(?:^|\\s)-d\\b", "reason": "shelve 삭제 금지"}`.
  어댑터를 바꾸면 `check_guard.py <adapter.json>`로 `guard-cases.json`이 `mismatches=0`인지 확인한다.
- **진행 표시**: `<stateDir>/ACTIVE`는 JSON `{"id", "session", "openedAt", "runtime"}`. 코디네이터가 전용 전환 명령으로 바꾼다.
  전환은 그 호출의 유일한 명령일 때만 동작한다.
  - 전용 명령 전체 인자와 사전 검사/실행 구분은 [공통 미션 절차](../../../shared/skills/mission.md#미션-전환-명령)를 따른다. 실행 전 훅은 ACTIVE를 변경하지 않는다.
  - `mission_control.py open <id> ...` — 없으면 만들고 이 세션에 건다. 다른 미션이 있으면 거부
  - `mission_control.py resume <id> ...` — 같은 미션을 이 세션에 다시 건다(압축·재시작 뒤)
  - `mission_control.py close <id> ...` — 지우고 `allow-once`도 지운다. 연 세션에서만(손상된 ACTIVE는 예외)
- **세션 범위**: ACTIVE의 `session`과 같은 세션만 막는다. 서브에이전트는 부모 세션의 `session_id`로 훅에 들어오므로 같이 막힌다(2026-10-15 실측). 다른 세션은 경고만 받는다.
- **1회 허용**: `<stateDir>/<id>/allow-once`에 명령 문자열이 한 줄로 있으면 그 명령은 한 번 통과하고 줄이 지워진다.
  사용자가 그 명령을 그 자리에서 승인했을 때만 쓴다. 읽다가 오류가 나면 막는다.
- **세션 기록**: open·resume 마커 때 `<stateDir>/<id>/sessions.tsv`에 `at action session transcript` 한 줄을 덧붙인다.
  transcript 는 훅 payload 의 `transcript_path`(없으면 빈 값). 미션 끝 기록 감사가 상태 CLI `transcripts`로 읽는다.
- **알림**: 가드가 꺼졌거나 경고가 있으면 사용자(`systemMessage`)와 모델(`additionalContext`) 모두에게 알린다.
- 가드는 걸림줄이다. 문자열을 조립하는 식의 의도적 회피까지 막지는 못한다. 실제 통제는 스킬의 멈춤 규칙이다.
- 훅은 PATH의 `python`으로 돈다. `check_guard.py --doctor`로 이 PC의 python이 진짜 실행되는지 확인한다.

## 서브에이전트 허용 목록

`agents.allow`가 있으면 `PreToolUse` 훅이 서브에이전트 파견 도구(Claude `Agent`·`Task`, Codex `spawn_agent`, Grok `spawn_subagent`)의
호출마다 이름을 목록과 비교한다. 미션 진행(ACTIVE) 여부와 상관없이 호출한 폴더에서 찾은 어댑터를 기준으로 한다.

- **이름 판정**: `fnmatch`(대소문자 구분, `*`·`?`·`[...]`)로 패턴 중 하나와 맞으면 통과한다. 호출 입력의 이름은 Claude·Grok `subagent_type`, Codex `agent_type`이다.
  이름을 생략한 호출은 런타임 기본 이름(Claude·Grok `general-purpose`, Codex `default`)으로 판정한다.
- **거부 이유**: 허용 패턴과 역할이 실제로 가리키는 이름을 알려 주므로, 에이전트는 허용되는 이름으로 다시 호출하고 맞는 역할이 없으면 다른 에이전트로 대신하지 않고 사용자에게 알린다.
  실제 이름은 `roles`의 값(`reviewLoopSkill` 제외)에 없는 역할의 chef 기본 이름(`chef:researcher`·`chef:dev-medium`·`chef:verifier`·`chef:code-reviewer`·`chef:reviewer-small`,
  `audit`는 review 따름, `implementSmall`은 Claude에서 implement 따름·Codex·Grok에서 `chef:dev-small`)과 옛 이름(`chef_*`, Grok의 `code-reviewer`·`chef:reviewer`)의 현재 이름 매핑을 반영한 값이다.
- **입력을 못 읽을 때**: 도구 입력이 JSON 문자열이면 파싱해서 읽는다. 파싱되지 않거나 객체가 아니거나 Grok `toolInputTruncated`로 잘렸으면 이름을 판정할 수 없으므로 막지 않고 알림만 낸다.
- **빈 배열**: `[]`은 모든 파견을 거부한다. 제한을 풀려면 `agents` 키를 지운다.
- **꺼짐**: 어댑터가 없거나 `agents` 키가 없으면 통과한다. 어댑터를 읽지 못하거나 `agents`·`allow`의 형식이 틀리면(객체·문자열 배열이 아님)
  도구를 막지 않고 `서브에이전트 허용 목록 꺼짐: <이유>` 알림만 낸다. `local.json`이 깨지면 그 파일은 무시되어 `local.json`에만 둔 제한이 빠질 수 있다.
- `/chef:doctor`가 위 실제 파견 이름이 모두 허용되는지 런타임별로 점검해, 막히는 이름이 있으면 경고한다.
- 훅 등록: Claude·Grok은 `hooks/hooks.json`의 `Agent|Task|spawn_subagent`, Codex는 `codex/hooks.json`의 `.*spawn_agent`(Codex 도구 이름에 namespace가 붙어 matcher가 전체 일치해야 한다).

## 슬롯

미션을 동시에 여러 개 돌리려면 워크스페이스를 나눈다. 슬롯 하나는 같은 스트림을 받는 별도 워크스페이스이고,
그 폴더에서 띄운 해당 런타임 세션 하나가 미션 하나를 맡는다. 세션마다 MCP 연결·에디터·`ACTIVE`가 따로라
서로 막지 않는다. 만들고 갱신하는 절차는 현재 런타임의 slots 스킬, 프로젝트별 준비 단계는 `adapter.md`의 "슬롯 준비".

| 키 | 뜻 |
| --- | --- |
| `slots.enabled` | 슬롯 사용 여부 |
| `slots.root` | 슬롯들을 두는 상위 폴더(`adapter-init`이 묻는다). PC마다 달라 `local.json`에 둔다 |
| `slots.names` | 슬롯 이름 목록(`[A-Za-z0-9][A-Za-z0-9_-]*`). 폴더는 `<root>/<name>`. 스크립트는 이 목록에 있는 이름만 받는다. 새 어댑터는 2~3개로 시작하고 동시 미션이 늘 때 이름을 더해 `create <이름>`으로 그때 만든다. 있는 슬롯을 줄이려고 지우지 않는다 |
| `slots.vcs` | 슬롯 작업 공간을 만들고 기준 리비전을 조회하는 VCS. `perforce` 또는 `none`(기본). `perforce`면 `stream`·`mainClient` 필수. `none`이면 `{baseline}`을 못 쓴다 |
| `slots.client` | 작업 공간 이름 규칙. `{name}`이 슬롯 이름으로 바뀐다. 사람마다 달라 `local.json`에 둔다 |
| `slots.stream` | 슬롯이 받을 스트림·브랜치 |
| `slots.p4` | `p4` 실행 명령(문자열 또는 실행 파일로 시작하는 인자 배열). 생략하면 `P4_BIN`, `CHEF_P4`의 절대 경로, 보강된 PATH와 표준 설치 위치 순으로 탐색한다. |
| `slots.mainClient` | 메인 워크스페이스 이름. 새 슬롯의 옵션을 따라 만들 원본이자 `{baseline}`을 조회하는 대상. `refresh`는 슬롯을 갱신하기 전에 이 워크스페이스를 먼저 head로 sync한다(`create`는 하지 않음). 사람마다 달라 `local.json`에 둔다 |
| `slots.prepare[]` | 슬롯 준비·갱신 단계 목록(아래) |
| `slots.beforeRun[]` | 슬롯 `create`·`create --resume`·`refresh`가 성공해 `ready`가 된 직후(`launchMode=prepare`)와, 새 미션 시작(`new`)·재개(`resume`) 직전에 실행할 단계 목록. 생략하면 실행하지 않음. 항목 형식은 `prepare[]`와 같음 |
| `slots.runtimeArguments` | CLI에 전달할 인자 배열(기본 `[]`). 개별 인자의 경계를 유지하고 모든 런타임의 `runtimes.<runtime>.slots.runtimeArguments`와 OS overlay를 지원한다. Hermes 현재 세션에는 새 CLI 인자를 전달하지 않는다 |
| `slots.codexProjectDocMaxBytes` | Codex 프로젝트 지침 읽기 한도(바이트). 양의 정수만 허용하며 bool·실수·문자열은 거부. 새 세션과 재개 모두 `-c project_doc_max_bytes=N` 적용. 생략하면 CLI 기본 동작 유지 |
| `slots.sandboxMode` | Codex 슬롯의 선택적 sandbox 설정. `read-only`·`workspace-write`·`danger-full-access`만 허용하며, 새 시작·재개에 `--sandbox <값>`을 전달함. 런타임별 값은 `runtimes.codex.slots.sandboxMode`에 둠. 생략하면 기존 CLI·프로젝트 설정을 상속하고 Claude·Grok에는 적용하지 않음 |
| `slots.codexAgentConfigs` | Codex native 역할 별칭별 `{configFile, description}` 객체. 선택 설정이며 기본은 빈 객체. 새 시작·재개에 역할 설정 파일과 설명을 명시적으로 등록함(아래) |
| `slots.refresh` | `refresh`가 다시 돌릴 `prepare[].id` 목록. `refresh --keep-local`을 쓰려면 `sync` 단계가 있어야 한다(그 단계 직후 `resolve`) |
| `slots.churn` | 엔진·에디터가 스스로 다시 쓰는 추적 파일의 목록(문자열 배열). 스트림 루트(`slots.stream`) 기준 상대 경로이고 p4 와일드카드 `*`(한 단계)·`...`(여러 단계) 허용, 배열이 아니거나 빈 문자열·절대 경로·`..` 구간이 있으면 오류. 로컬 변경 검사에서 빠지고 `refresh` 때 `#have` 내용으로 되돌린다. `slots.vcs`가 `perforce`일 때만 쓴다 |
| `slots.editor` | 기존 에디터 명령 문자열. `editorArgv`와 동시에 지정할 수 없다. 셸 실행 문법을 자동 변환하지 않는다 |
| `slots.editorArgv` | 에디터 실행 파일·인자 배열. 첫 인자는 비어 있지 않아야 하며 각 인자에 변수를 확장한다. 공백·빈 후속 인자 경계를 보존한다. OS별 실제 실행 파일은 개인 설정에 둔다 |
| `slots.terminal` | `auto`(기본)·`foreground`·`wt`·`macos-terminal`·`gnome-terminal`·`konsole`·`tmux`. auto는 Windows에서 WT, POSIX에서 현재 TTY를 사용한다. GUI 없는 별도 세션에는 명시적으로 tmux를 선택한다. Hermes는 현재 native 세션 경로를 사용한다 |
| `slots.requirements` | 선택 객체 `{"executables":["{python}"],"files":["{main}/tools/project.py"]}`. 상대 경로는 메인 프로젝트 기준이다. 실행 파일·정규 스크립트 존재를 create·refresh·run의 변경 작업과 Hermes 진입 전에 검사한다. 파일·도구가 없으면 시작하지 않는다 |

`slots.prepare[]`·`slots.beforeRun[]` 항목은 다음 키를 가진다. `group` 번호 순서로 돌고, 같은 번호의 단계끼리는 병렬이다.
한 그룹이 실패하면 뒤 그룹은 돌지 않는다.

| 키 | 뜻 |
| --- | --- |
| `id` | 단계 이름. `slots.refresh`와 로그·시간 기록이 이 이름을 쓴다 |
| `group` | 실행 순서 번호(기본 0) |
| `run` | 기존 명령 문자열. `argv`와 하나만 지정한다 |
| `argv` | 실행 파일로 시작하는 문자열 배열. 각 항목을 개별 확장하고 셸 없이 실행한다. 셸이 필요하면 실행 파일로 명시한다 |
| `cwd` | 작업 폴더 템플릿. 없으면 현재 폴더 |
| `exclusive` | `true`면 같은 그룹이어도 다른 `exclusive` 단계와 동시에 돌지 않는다(에디터 같은 공유 자원) |
| `okExit` | 성공으로 칠 종료 코드 목록(기본 `[0]`, robocopy는 `[0, 1, 2, 3]`) |

`run`·`cwd`·`editor`의 템플릿 변수:

| 변수 | 값 |
| --- | --- |
| `{name}` | 슬롯 이름 |
| `{slot}` | 슬롯 폴더(`<root>/<name>`) |
| `{client}` | `slots.client`로 만든 슬롯 작업 공간 이름 |
| `{main}` | 메인 프로젝트 폴더(`vars.main`, 없으면 어댑터를 찾은 폴더) |
| `{log}` | 이 단계의 로그 파일 경로(`prepare`·`beforeRun` 단계에서 사용) |
| `{launchMode}` | `beforeRun` 전용. 새 미션 시작은 `new`, 미션 재개는 `resume`, 슬롯 `create`·`refresh` 직후는 `prepare` |
| `{sessionId}` | `beforeRun` 전용. CLI 재개 명령과 같은 검증된 기존 세션 UUID. 새 미션·`--new-session`·기존 UUID가 없어 새 세션으로 이어받는 경우 빈 문자열. `prepare`에서는 항상 빈 문자열 |
| `{slotLockPath}` | `beforeRun` 전용. 현재 슬롯의 canonical 잠금 파일 절대 경로 |
| `{slotLockOwnerPid}` | `beforeRun` 전용. 해당 잠금을 보유한 슬롯 실행 프로세스의 decimal PID |
| `{baseline}` | 메인 워크스페이스가 받은 CL 번호(Perforce: `slots.mainClient`의 `changes -m1 //<mainClient>/...#have`). `create`는 메인이 지금 받은 CL이고, `refresh`·`run --refresh`는 메인을 먼저 head로 sync한 뒤 조회하므로 head다. 템플릿에 있을 때만 한 번 조회하고, `slots.vcs`가 없으면 쓸 수 없다 |
| `{memoryDir}` | 슬롯 세션의 Claude 자동 메모리 폴더 |
| `{mainMemoryDir}` | 메인 세션 쪽 Claude 자동 메모리 폴더 |
| `{python}` | Chef를 실행한 Python의 `sys.executable`. 개인 vars로 덮어쓰지 않는다 |
| `{plugin}` | 현재 설치본 플러그인의 절대 경로 |
| `{<키>}` | `vars`의 키(예: `{editorExe}`). 정의 안 된 변수를 쓰면 실행 전에 오류 |

`beforeRun`은 슬롯 상태·ACTIVE·재개할 세션 검사를 통과한 뒤, 슬롯 잠금 안에서 에디터·CLI를 열기 전에 한 번 실행한다.
외부 준비 도구는 `--lock-path "{slotLockPath}" --lock-owner-pid "{slotLockOwnerPid}"`로 기존 잠금 문맥을 받을 수 있다.
잠금 파일은 UTF-8이며 첫 tab 앞 필드는 decimal owner PID다. tab 뒤 메타데이터 형식은 해석하지 않는다.
도구는 전달 PID와 파일 owner, 살아 있는 조상 프로세스 소유를 확인하고 잠금을 삭제하거나 다시 획득하지 않는다.
`--dry-run`의 값은 실행 계획이며 실제 잠금 보유 증거가 아니다. 이 문맥은 `prepare`에는 제공하지 않는다.
`run --refresh`는 갱신을 먼저 끝낸다. 단계 실패 시 실행을 중단하고 로그를 알리며, 기존 `ready` 상태는 유지한다.
`create`·`create --resume`·`refresh`에서는 준비·갱신 단계가 성공해 슬롯이 `ready`가 된 직후, 슬롯 잠금 안에서 슬롯마다 한 번 실행한다(`launchMode=prepare`).
실행 전에 슬롯을 직접 열어도 beforeRun이 만드는 정의·지침이 맞도록 하기 위해서다. `run --refresh`의 갱신은 `prepare`로 부르지 않고 뒤이은 `new` 호출만 부른다.
실패하면 슬롯은 `ready`로 남기고 그 슬롯을 `failed`로 보고하며 종료 코드 1이다. 다른 슬롯은 계속 진행한다.
`--dry-run`은 단계만 표시하고 실행·파일 쓰기는 하지 않는다. 로그와 시간 기록의 단계 이름은 `beforeRun.<id>`이고,
로그는 `<stateDir>/slots/beforeRun/`에 두어 준비·갱신 로그와 분리한다.
출력 로그는 `<이름>-beforeRun.<id>.out.log`, 명령의 `{log}`는 `<이름>-beforeRun.<id>.log`,
시간 기록은 같은 디렉터리의 `<이름>-timing.tsv`다.
런타임별 명령은 기존 `runtimes.<런타임>.slots.beforeRun`에 두고, 프로젝트별 도구·경로·지침 내용은 어댑터에서 관리한다.
`beforeRun.run`에서 실행 문맥을 넘기려면 `--launch-mode "{launchMode}" --session-id "{sessionId}"`처럼 값에
큰따옴표를 쓴다(JSON 문자열에서는 `\"`로 이스케이프). 빈 `sessionId`도 빈 인자 하나로 전달된다.
`sessionId`는 새 CLI 세션 ID를 예약하지 않으며, 기존 슬롯·ACTIVE·세션 검사를 대신하지 않는다.

`runtimeArguments`는 셸 명령 문자열이 아니다. 배열이 아닌 값(`null` 포함), 문자열이 아닌 항목, NUL·CR·LF·잘못된 UTF-8은 거부한다.
빈 문자열도 인자 하나로 유지하고, 공백·유니코드·따옴표·세미콜론은 Windows Terminal 전달 과정에서 보존한다.
각 항목은 `{name}`·`{slot}`·`{client}`·`{main}`·`vars`와 Claude의 메모리 변수를 기존 템플릿 규칙으로 확장한다.
정의되지 않은 변수와 확장 뒤 잘못된 문자열은 실행 전에 거부하며, `beforeRun` 전용 변수와 `{baseline}`은 제공하지 않는다.
인자의 의미와 TOML 등 내부 형식은 해당 런타임 CLI가 해석한다. 셸 인용부호를 배열 항목 바깥에 덧붙이지 않는다.
템플릿은 문자열을 그대로 치환하므로, TOML 문자열 안의 경로는 TOML의 인용·이스케이프 규칙도 충족해야 한다.
기존 런타임·로컬 병합 순서를 따르며 배열은 합치지 않고 대체하므로, 로컬 `[]`로 추가 인자를 비울 수 있다.
프로젝트 설정 파일을 변경하지 않고 Codex의 세션 설정을 전달하는 가상 예:

```json
{
  "runtimes": {
    "codex": {
      "slots": {
        "runtimeArguments": [
          "-c",
          "hooks.PreToolUse=[{matcher=\".*\",hooks=[{type=\"command\",command='python \"{slot}/tools/example_guard.py\"'}]}]"
        ]
      }
    }
  }
}
```

이 배열은 공식 슬롯 `run` 경로의 CLI 실행에만 적용하며, `prepare`·`beforeRun`·에디터 명령에는 붙이지 않는다.
Claude·Grok도 각 런타임 슬롯 설정에서 같은 배열 계약을 사용한다. 훅 지원 여부·설정 키 등은 사용하는 CLI 버전의 계약을 따른다.

`codexAgentConfigs`의 각 값은 `configFile`·`description` 두 키만 가지며 둘 다 비어 있지 않은 문자열이어야 한다.
별칭은 영문자·숫자·`_`·`-`만 허용하며, `chef:dev-medium`처럼 콜론 하나로 나뉜 두 부분도 허용한다(콜론이 든 별칭은 `-c` 키에서 따옴표로 감싼다). `agents`의 스칼라 설정 이름인 `max_threads`·`max_depth`·
`job_max_runtime_seconds`·`default_subagent_model`·`default_subagent_reasoning_effort`·`enabled`·
`interrupt_message`·`max_concurrent_threads_per_session`은 별칭으로 쓰지 않는다.
등록 필드와 현재 스칼라 키는 [공식 Codex 설정 스키마](https://learn.chatgpt.com/docs/config-schema.json)를 따른다.

`configFile`은 기존 `{slot}`·`vars` 템플릿을 확장한 뒤 절대 경로여야 한다.
`run` 경로에서만 `beforeRun`이 성공한 뒤 에디터·CLI를 열기 전에 파일 존재·읽기 가능 여부를 확인한다. 콜백이 없으면 기존 파일을 확인한다. `prepare` 호출 뒤에는 이 파일 검사를 하지 않는다.
`--dry-run`은 파일이 아직 없어도 매핑 인자를 표시하며 파일을 만들지 않는다. 파일 내용은 Codex가 해석한다.
각 항목은 별도 argv의 `-c agents.<별칭>.config_file="<경로>"`와 `-c agents.<별칭>.description="<설명>"`으로 전달한다.
문자열은 TOML 형식으로 이스케이프하며 런타임별 값은 `runtimes.codex.slots.codexAgentConfigs`에 둘 수 있다.
생략하면 기존 실행 인자를 유지하고, Claude·Grok에는 등록 인자나 파일 검사를 적용하지 않는다.

슬롯 목록과 상태는 메인 프로젝트의 `<stateDir>/slots.tsv`에 둔다. 열은 탭으로 구분한다.

| 열 | 뜻 |
| --- | --- |
| `name` | 슬롯 이름(키) |
| `path` | 슬롯 폴더 |
| `client` | 슬롯 작업 공간 이름 |
| `created` | 만든 시각 |
| `synced` | 마지막 동기화 시각 |
| `state` | `creating`(만드는 중) / `ready` / `failed:<단계 id>`(만들다 실패) / `failed:refresh:<단계 id>`(갱신 실패. `churn`은 churn 되돌림 실패, `resolve`는 `--keep-local` 뒤 남은 충돌). 만들다 실패·중단된 슬롯은 `create --resume`, 갱신 실패는 `refresh`로 복구한다. `run`·`refresh`는 그 밖의 상태면 거부한다. 같은 슬롯 동시 조작은 `<stateDir>/slots/<이름>.lock`으로 막는다. `--discard-local` 플래그를 포함한 동작은 공통 slots 절차가 정본이다 |

슬롯 폴더도 레지스트리 `roots`에 넣어야(예: `<root>/*`) 슬롯에서 띄운 세션이 어댑터를 찾는다.

## adapter.md 에 들어갈 절

- VCS: 작업 단위(CL·브랜치) 만들기, 파일 열기·추가, shelve, 설명 인코딩 함정, 남이 연 파일 처리
- submit 정책: 금지 / 사전 위임 시 게이트 명령 / 예외
- 검증: compile·test·console·server-test 명령, 공유 자원(에디터) 규칙, 로컬에서 안 돌리는 검증
- 에디터 수명: 필요 시 실행 명령·프로젝트 연결과 준비 확인·기존 인스턴스 재사용·직접 연 인스턴스 식별과 정상 종료.
  결과 수집 뒤나 실패·질문 대기로 빠질 때 정리하고, 저장 확인·진행 중 작업은 강제 종료하지 않음.
- 사람 확인 항목: 기계로 판정 못 하는 변경 종류(예: 프리팹, 테이블 값, UI 손맛)
- 독점 락 파일 종류
- 하네스 문서 위치(규칙·라우터·워크플로)
- 슬롯 준비(슬롯을 쓰면): 새 워크스페이스가 빌드·검증 가능해지기까지의 단계와 실측 시간

## Codex 슬롯 준비

Codex에 필요한 준비 단계는 `runtimes.codex.slots.prepare`에 둔다. Claude의 설정 복사·메모리 junction을
Codex에 그대로 쓰지 않는다. 예시 어댑터(`adapters/example/`)는 외부 도구 검사·소스/도구 복사·setup·verify의 공통 단계를 사용한다. Claude 메모리 연결은 Claude 전용 beforeRun에만 둔다.
`{memoryDir}`·`{mainMemoryDir}`는 Claude 전용 변수다. Codex 준비 단계에서는 쓰지 않는다.
Codex 전용 MCP 설정과 AGENTS.md가 로컬 파일이면 준비 단계에 실제 복사 절차를 명시한다.
Codex의 기본 셸은 Windows에서 PowerShell로 해석하고, 명시된 shell이 있으면 그 값을 따른다.
가드는 도구 차단 보조 수단이며 write_stdin의 후속 입력이나 의도적 우회를 통제하지 않는다.
# Hermes와 이식 가능한 준비 명령

Hermes는 `runtimes.hermes`에 런타임 덮어쓰기를 두고 위 OS·런타임 병합을 사용한다. 프로젝트별 경로·도구·스크립트는 개인 어댑터에 유지한다. [Hermes 안내](../../../hermes/README.md)를 따른다.

`slots.prepare`와 `slots.beforeRun` 단계는 기존 `run` 문자열 또는 새 `argv` 문자열 배열 중 하나만 지정한다. `argv`는 각 인자에 변수를 개별 확장하고 셸을 거치지 않는다. 셸 문법이 필요하면 해당 셸을 명시적으로 호출한다. 예: `{"id":"setup","argv":["{python}","{main}/tools/setup.py","--slot","{slot}"]}`. `run`의 기존 실행 의미는 유지한다. `cwd`, `group`, `exclusive`, `okExit`는 두 형식 모두 동일하다.

## 이식 가능한 준비 도구

`{python} {plugin}/skills/slots/scripts/prepare.py`는 다음 동작을 제공한다.

- `check --executable <실행파일> --file <스크립트>`: 반복 옵션으로 필요한 도구·정규 입력을 먼저 검사한다. 필수값은 `slots.requirements`에도 선언해 main sync·슬롯 생성보다 먼저 확인한다.
- `copy --source <폴더> --destination <폴더>`: 링크·특수 노드는 복사하지 않는다. 기존 파일이 다르면 복사 시작 전에 실패하고, 같은 파일·기존 추가 파일은 유지한다. 삭제·덮어쓰기·자동 갱신은 하지 않는다. 부모 디렉터리의 정상 경로 별칭은 해석하되 복사 트리 안의 링크는 거부한다.
- `link --source <폴더> --destination <링크>`: Windows native junction / POSIX directory symlink. 올바른 링크는 재사용하고 다른 링크·파일·폴더는 교체하지 않는다. 없는 원본 폴더 생성은 명시적인 `--create-source`일 때만 한다.
- copy와 link의 `--dry-run`은 파일·링크·폴더를 만들지 않는다.

캐시 copy에 `--source-fingerprint <json>`과 `--expected-fingerprint <json>`를 함께 지정하면 metadata를 먼저 비교한다. `os`(`platform.system().lower()`), `architecture`(`platform.machine().lower()`), `toolVersion`, `backend`, `inputs`의 비어 있지 않은 문자열과 전체 JSON 내용이 같아야 한다. `inputs`에는 프로젝트가 계산한 입력 digest를 기록한다. 누락·파손·불일치는 복사를 생략하고 exit 0을 반환하므로, 다음 import 단계는 항상 실행하도록 구성한다. 일반 소스 복사에는 fingerprint 옵션을 붙이지 않는다. 예시 `adapters/example/cache-copy.example.json`은 선택적인 Library 캐시 단계이며 실제 빌드 도구와 fingerprint 생성은 개인 어댑터에 둔다.
