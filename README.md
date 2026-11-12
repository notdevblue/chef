# chef — 개인 Claude Code · Codex CLI · Grok CLI 플러그인

개인 규칙, 파견용 에이전트, 미션 오케스트레이션을 한 플러그인으로 묶는다. 프로젝트 저장소에는 아무것도 올리지 않는다.
Grok Bot은 바깥 코디네이터로 상태를 읽고, 메인 또는 슬롯의 실행 봇에 작업을 넘길 수 있다. Claude와 Codex는 선택 경로다.

개발·설치·업데이트의 기준은 [공개 저장소](https://github.com/notdevblue/chef)다.
공개 배포본에는 회사·고객·비공개 프로젝트 관련 자료와 지침을 절대 두지 않는다. 회사 규칙을 복사·요약·익명화해 공통 규칙으로 옮기지 않는다.
예시는 모두 가상이며, 실제 프로젝트 설정은
저장소 밖의 개인 어댑터나 Git에서 제외된 파일에 둔다. 기여할 때는 [공개 저장소 경계](AGENTS.md#공개-저장소-경계)를 따른다.

| 구성 | 위치 | 하는 일 |
| --- | --- | --- |
| 공통 스킬 절차 | `plugins/chef/shared/skills/` | 다섯 스킬의 단계·승인·검증·완료 정본. 런타임 진입점은 도구·역할·경로 차이만 둠 |
| 개인 규칙 | `plugins/chef/rules/` | 플러그인 설정 `inject_rules`가 `on`이거나 `shared`일 때만(`shared`는 `personal-rules.md`를 뺀 나머지), 세션 시작·재개·압축 때 `SessionStart` 훅이, 서브에이전트 시작 때 `SubagentStart` 훅이 컨텍스트에 넣는다(예전 `~/.claude/CLAUDE.md`) |
| 에이전트 | `plugins/chef/agents/` | `chef:researcher`, `chef:dev-small`, `chef:dev-medium`, `chef:verifier`, `chef:code-reviewer`, `chef:reviewer-small` |
| `/chef:mission` | `plugins/chef/skills/mission/` | 컨텐츠 확인 → PoC → 구현 → 검토, 사람 확인 A·B·C. 상태 파일은 `scripts/mission_state.py`로만 쓰고, 끝에 기록 감사 |
| `/chef:distill` | `plugins/chef/skills/distill/` | 대화 기록에서 사용자 피드백을 뽑아 가이드·메모리·개인 규칙·플러그인 반영 후보로 분류, 고른 것만 반영. 미션은 확인 C 직전에 자동 |
| `/chef:adapter-init` | `plugins/chef/skills/adapter-init/` | 프로젝트 조사해 어댑터 초안, 슬롯 폴더 묻기 |
| `/chef:slots` | `plugins/chef/skills/slots/` | 병렬 미션용 슬롯 워크스페이스 만들기·갱신·실행·삭제 |
| `/chef:doctor` | `plugins/chef/skills/doctor/` | 읽기 전용 점검: python 스텁, 레지스트리·어댑터·local.json, guard-cases, ACTIVE 끊김 의심(닫기는 제안만), Unity 경로, 저장소·설치본 버전. Codex는 `chef-doctor` |
| Grok 바깥 층 | `grok/` | 등록하지 않는 초안. 읽기 전용 셸 호출 목록. 실행 절차는 `plugins/chef/grok/` |
| Grok | `plugins/chef/grok/` | 등록 스킬 여덟 개와 native 역할 여섯 개. 메인·슬롯 CLI가 역할별 하위 에이전트를 조율한다. SessionStart `new`·`load`와 미션 가드는 공유 훅 규칙을 따른다. `chef-watch`·`chef-handoff`는 등록하지 않는 안내 |
| 미션 가드 | `plugins/chef/hooks/mission_guard.py` | 미션 중 submit 등 금지 명령 차단(걸림줄) |
| 서브에이전트 허용 목록 | `plugins/chef/hooks/subagent_allow.py` | 어댑터 `agents.allow`(이름 glob 목록)에 맞지 않는 서브에이전트 파견을 거부. 키가 없으면 제한 없음, 미션 진행과 무관 |
| 질문 무결성 | `plugins/chef/hooks/question_integrity.py` | 미션 진행·완료·공유 전에 질문 정본·생성본 검사. 참여 세션의 답변 종료 전에도 전체 검사 |
| 어댑터 | `adapters/<이름>/` | 프로젝트별 값(`adapter.json`)과 절차(`adapter.md`) |

Claude·Codex 훅은 규칙 주입·미션 가드·서브에이전트 허용 목록·질문 무결성 4종이다. Grok은 SessionStart `new`·`load` 규칙 전달, 미션 가드, 서브에이전트 허용 목록을 지원한다. 세션 제목·메모리 안내·한국어 답변 차단 훅은 제공하지 않는다.
슬롯 실행에서 지정하는 세션 이름은 유지한다.

미션의 `confirm-*.md`·`ask-*.md`는 이번 변경·남은 질문·최신 승인 대상·승인 범위를 보여 주고, 개정할 때 현재 상태로 고쳐 쓴다.
현재 기술 명세·실행 근거는 `details/<같은 파일명>.md`, 질문 정의·답 원문·날짜·대상 개정·승인 범위의 생성 이력은
`details/history/<같은 파일명>.md`와 기존 개정 보관에 둔다. `report.md`의 전체 근거·감사 이력은 `details/report.md`에 둔다.
[문서별 양식과 완성 예시](plugins/chef/skills/mission/references/human-writing.md)에서
질문·설계 승인·보고·답변 완료 중 해당 자료만 고른다. 작성자는 원문·최신 답과 상태로 쓰고 스스로 대조해 고친다.
현재 본문에는 승인할 스키마·통신 구조를 한 벌로 직접 싣고, 작성자가 문서 `body`에 끝난 문항의 실제 현재 선택·유효 조건을 짧게 쓴다.
다른 절에서 명확한 선택은 별도 요약을 만들지 않는다. 생성기는 완료 답을 현재 문서에 재출력하지 않는다.
기존 질문·답·승인 범위는 정본과 이력에 온전히 보존하며, 코디네이터의 사후 윤문에 의존하지 않는다.
미션 루트의 `confirm-*.md`·`ask-*.md`와 대응하는 상세는 대시보드에 자동 표시하며, 수정 뒤에도 최신 내용을 연다.
질문 표시와 승인·검토 완료는 별개다. 보고와 그 밖의 자료는 검토 후 상태 CLI의 `document share`로
각 경로를 등록하고, 수정하면 다시 검토해 재공유한다.

**언제 미션을 쓰나** — 여러 마일스톤에 검증·사람 확인 게이트가 있는 작업(티어 M·L)만 `/chef:mission`으로 연다.
한두 파일짜리 수정이나 하나로 끝나는 작업(티어 S)은 미션 없이 직접 하거나 한 번 파견하고, 통과할 때까지 반복은 루프로 돌린다.
`init --tier S`는 `--force-tier` 없이 거부된다. 슬롯은 새 어댑터 기준 2~3개로 시작하고 필요할 때 늘린다.

미션 시작·재개는 런타임 스킬의 `mission_entry.py`를 먼저 실행한다. 개인 레지스트리의
`mission-entry.json`에 명시적으로 신뢰한 외부 이벤트를 등록하면 어댑터가 없어도 준비·선택·등록 슬롯 인계를 처리한다.
공통 코드는 이벤트 결과와 경로를 다시 검증하며, 해당 root의 open·resume 가드는 10분짜리 진입 receipt를 검사한다.
슬롯의 실제 실행 준비는 기존 `slots.beforeRun`과 슬롯 잠금을 쓴다. 등록·확장 원본·receipt는 설치 폴더 밖에 둬 업데이트 때 보존한다.
등록이 없는 환경의 기존 동작은 유지한다. [CLI·등록·JSON 계약](plugins/chef/skills/mission/references/mission-entry.md)을 따른다.

## Codex CLI에 설치

Claude용과 같은 저장소에서 설치하며, Codex용 진입점은 `plugins/chef/.codex-plugin/plugin.json`이다.
스킬·역할·개인 규칙은 `plugins/chef/codex/`, 가드·슬롯 구현은 Claude용과 공유한다.
CLI 0.162.1에서 별도 임시 사용자 폴더로 마켓플레이스 등록·설치를 검증했다.

PowerShell에서 실행한다.

```powershell
codex plugin marketplace add <repo>
codex plugin add chef@chef-codex

$env:HAN_ADAPTERS_DIR = '<repo>/adapters'
$env:HAN_CODEX_INJECT_RULES = 'on'
codex -C <project>
```

`HAN_CODEX_INJECT_RULES`는 개인 규칙을 쓸 때만 설정한다. 기본값은 꺼짐이고 `HAN_RULES_DISABLE=1`이 우선한다.
값 `on`(`true`·`1`·`yes`)은 `codex/rules/`의 규칙을 모두 넣고, `shared`는 `personal-rules.md`만 빼고 문서 규칙·메인 세션 규칙·사람용 문서 자료 안내를 넣는다.
Grok의 `HAN_GROK_INJECT_RULES`도 같은 값을 쓴다.
셸에서 지정한 환경변수 값은 Codex를 시작할 때 전달되어야 한다. SessionStart 훅이 부모 셸 환경을 바꾸지는 않는다.
`HAN_ADAPTERS_DIR`은 이 셸 값이 우선한다. 지속 설정은 새 config key 없이 기존 Codex 설정의
`[shell_environment_policy.set]`에 둘 수 있다. Codex가 `CODEX_HOME`을 설정했으면 `<CODEX_HOME>/config.toml`, 아니면 `~/.codex/config.toml`이다.

```toml
[shell_environment_policy.set]
HAN_ADAPTERS_DIR = "D:/Work/chef/adapters"
```

이 설정을 읽는 폴백에는 표준 라이브러리 `tomllib`이 필요해 Python 3.11 이상이어야 한다.
`adapters/projects.json`·`adapters/<이름>/local.json`은 아래 Claude 설치 안내와 같은 PC별 파일을 쓴다.

새 세션의 `/hooks`에서 플러그인의 훅을 검토하고 신뢰한다. hooks 기능을 꺼둔 환경이면
`codex --enable hooks`로 시작한다. 훅이 바뀌면 다시 신뢰해야 한다.
미션 시작 마커의 셸 `echo` 출력만으로 훅 실행을 판단하지 않는다. 훅의 `가드 켜짐` 안내와 ACTIVE의 미션 id 및
현재 세션 id가 일치하는지 확인한 뒤에만 진행한다.
훅 계약과 신뢰 절차는 [공식 훅 문서](https://learn.chatgpt.com/docs/hooks)를 따른다.

Codex 프롬프트에서 사용한다.

```text
$chef:chef-mission <작업>
$chef:chef-distill
$chef:chef-slots status
$chef:chef-adapter-init
```

Codex 슬롯의 `run`은 새 Windows Terminal 탭에서 `codex`와 `$chef:chef-mission`을 실행한다.
`run <슬롯> --resume`으로 진행 중인 미션의 기록된 대화를 새 탭에서 이어받는다. 미션 대시보드도
`codex/skills/chef-slots/scripts/dashboard.py`로 실행할 수 있다.
대시보드는 공유된 ask·confirm·보고와 답 이력을 읽는 화면이다. 답과 승인은 현재 미션 대화에서 받는다.
슬롯 설정 없이 메인 프로젝트에서도 실행한다. 현재 프로젝트와 어댑터의 메인·등록 슬롯,
개인 레지스트리의 exact·glob roots에서 미션 기록이 있는 작업 공간을 요청마다 발견한다. glob은 경로 한 단계씩만 확장하며
등록 범위 밖의 드라이브나 하위 폴더를 재귀 검색하지 않는다.
질문은 `questions/<id>.json`을 정본으로 두고 `question publish`·`question answer`가 Markdown을 생성한다.
대시보드는 문항 상태만 읽으며 누락·손상·생성본 불일치는 `기록 확인 필요`로 구분한다. [질문 기록 절차](plugins/chef/skills/mission/references/question-records.md)를 따른다.
기존 사용자 입력·답 이력은 보존하며 대시보드에서 답을 저장·전달하거나 중단된 세션을 자동 재개하지 않는다.
`chef-distill`은 Codex 대화 기록에서 피드백을 추출하며, 미션 확인 C 직전에도 실행한다.
SessionStart 훅이 대화 기록 경로를 컨텍스트에 전달하고, 처리 로그는 `<CODEX_HOME>/han/distill/log.tsv`
(`CODEX_HOME`이 없으면 `~/.codex`)에 둔다. 사용자가 고른 후보만 반영한다.
어댑터는 가까운 폴더부터 `.codex/orchestrator/adapter.json` → 같은 폴더의 `.claude/orchestrator/adapter.json`,
그다음 개인 레지스트리를 찾는다. 레지스트리는 시작 셸의 `HAN_ADAPTERS_DIR`, 없거나 빈 경우 Codex 설정의
`shell_environment_policy.set.HAN_ADAPTERS_DIR`, 둘 다 없을 때 저장소에서 직접 실행 중이면 `<저장소>/adapters` 순이다.
Codex용 차이는 `runtimes.codex`에 두며, 병합 순서는
adapter → adapter.runtimes.codex → local → local.runtimes.codex다.
예시 어댑터(`adapters/example/`)의 Codex 준비 단계는 sync·library·junction·import·verify이며, Claude 설정·메모리 복사는 하지 않는다.
프로젝트 AGENTS.md와 Codex용 Unity MCP 연결은 해당 프로젝트에 준비되어 있어야 한다.
로컬 Codex 설정·개인 AGENTS.md 복사가 필요하면 어댑터 준비 단계에 명시한다.
매번 실행 직전에 갱신할 로컬 지침은 `runtimes.codex.slots.beforeRun[]`에 둔다. 새 미션 시작·재개 모두
슬롯 검사 후 에디터·CLI 실행 전에 한 번 돌며, 실패하면 세션을 열지 않는다. 단계 형식은 기존 `prepare[]`와 같다.
`beforeRun`은 슬롯 `create`·`create --resume`·`refresh` 직후에도 `{launchMode}`=`prepare`로 불린다. `new`·`resume`만 처리하던 외부 beforeRun 명령은 `prepare`도 받아야 한다.
Codex 프로젝트 지침 읽기 한도는 선택 설정 `slots.codexProjectDocMaxBytes`(양의 정수, 바이트)로 새 세션·재개에 적용한다.
생략하면 기존 CLI 동작을 유지한다. 전체 계약은 [어댑터 슬롯 설정](plugins/chef/codex/skills/chef-mission/references/adapter-schema.md#슬롯)에 있다.
슬롯에 준비한 native 역할 파일은 `slots.codexAgentConfigs`의 별칭별 `{configFile, description}`으로 명시 등록한다.
새 세션·재개 모두 준비 단계가 끝난 뒤 파일을 확인하고, 역할 파일 경로와 설명을 Codex 설정 인자로 전달한다.

`codex/agents/*.toml`은 역할별 정의이며 이름은 `chef:researcher`·`chef:dev-small`·`chef:dev-medium`·`chef:verifier`·`chef:code-reviewer`·`chef:reviewer-small`이다. 등록된 역할만 파견하고, 등록이 없으면 다른 에이전트로 대신하지 않고 사용자에게 알린다.
research와 심층 review(`chef:code-reviewer`)는 부모 모델·추론 수준을 상속한다. 중간 구현 역할은 `gpt-6.1-sol` / high,
작은 구현 역할은 `gpt-6-luna` / high, 작은 리뷰 역할 `chef:reviewer-small`은 `gpt-6.1-terra` / high, 검증 역할 `chef:verifier`는 `gpt-6-luna` / high로 고정한다.
역할 파견은 `~/.codex/agents/`에 복사해 등록한 이름이 전제다. 등록은 사용자가 정하며 이 플러그인 설치만으로 전역 파일을 쓰지 않고, 등록할 때 기존 파일을 덮어쓰지 않는다.
사용자가 플러그인을 업데이트할 때 등록한 사본도 플러그인의 `codex/agents/`에서 다시 복사해야 하며, chef doctor가 낡은 등록·빠진 역할·플러그인에 없는 `chef-` 역할 파일을 보고한다. 옛 파일명(`chef-developer-small.toml`·`chef-developer-medium.toml`)은 `chef-dev-small.toml`·`chef-dev-medium.toml`로 바뀌었으므로 등록한 옛 사본은 지우고 새 파일을 복사한다.
Claude와 Codex는 미션 상태·슬롯 잠금을 공유하므로 같은 슬롯의 미션을 동시에 이어받지 않는다.

업데이트는 마켓플레이스를 갱신하고 플러그인을 다시 설치한 뒤 새 세션에서 훅을 확인한다.
CLI 옵션은 `codex plugin marketplace --help`와 `codex plugin add --help`로 확인한다.
패키징은 [공식 플러그인 문서](https://developers.openai.com/plugins/build/plugins)의 Codex 호환 형식을 쓴다.

## Grok Bot·CLI에서 보기

Grok 코디네이터는 상태를 읽고 메인 또는 슬롯의 실행 봇에 작업을 넘긴다. 로컬 상태·슬롯 조회는 chef 스크립트로 하고,
저장소 코드 작업은 프로젝트의 실행 규칙을 따른다. Claude(`/chef:mission`)와 Codex(`$chef:chef-mission`)는 선택 경로다.
Grok SessionStart는 `new`·`load`를 지원하고, 미션 가드는 `open`·`resume`·`close` 마커를 처리한다. native chef는 `~/.grok/plugins/chef`에서 공개 체크아웃의 `plugins/chef`를 읽는다. `chef-status`와 `chef-doctor`는 읽기만 하고, 봇을 깨우는 인계는 `chef-mission-brief`가 맡는다.
실행 절차는 `chef-run`이다. Main 또는 slotN의 Grok CLI는 오케스트레이터로서 native 역할에 조사·구현·검증·리뷰·감사를 맡긴다. 역할과 모델·effort 계약은 [Grok 실행 역할](plugins/chef/grok/README.md#실행-역할)에 있다.

프로젝트 규칙은 그 저장소의 `AGENTS.md`·`CLAUDE.md`와 `.claude/rules/`다. Grok CLI는 이를 이미 읽는다.
전역 실행 규칙은 `~/.grok/rules/`에 두며, CLI가 읽은 규칙은 `grok inspect`에서 확인한다. `chef-run`은 그 규칙을 복제하지 않는다.

조회 스크립트는 `plugins/chef/grok/scripts/han_state.py`다. 어댑터는 가까운 폴더부터
`.grok/orchestrator/adapter.json` → `.claude/orchestrator/adapter.json` → `.codex/orchestrator/adapter.json`,
그다음 `HAN_ADAPTERS_DIR`, `HAN_REPO/adapters`, 저장소에서 직접 실행할 때 `<저장소>/adapters` 순이다.
Claude 플러그인 설정과 Codex `config.toml`은 읽지 않는다. 기본 조회는 `adapter.json` → 그 파일의 `runtimes.grok` → `local.json` → 그 파일의 `runtimes.grok` 순으로 병합한다. `brief --runtime claude|codex`는 같은 발견 순서를 유지하며 인계할 런타임의 병합으로 상태를 읽는다.

머지된 저장소를 Grok 셸이 닿는 PC에 받은 뒤 native chef 플러그인을 등록한다. Grok Bot 라이브러리는 별도로 아래 여덟 스킬을 등록한다. `claude plugin`·`codex plugin` 명령은 쓰지 않는다.

1. 저장소 체크아웃 경로 `<repo>`를 정한다.
2. 그 PC의 Grok 셸에 `HAN_ADAPTERS_DIR=<저장소>/adapters` 또는 `HAN_REPO=<저장소>`를 둔다.
   프로젝트 안에 `.grok/orchestrator/adapter.json`이 있으면 레지스트리보다 그 파일이 우선한다.
3. `~/.grok/plugins/chef`를 `<repo>/plugins/chef`으로 연결한다. Windows에서는 junction, POSIX에서는 symlink를 쓴다. 이미 `~/.grok/plugins/chef`가 있으면 실제 대상을 확인하고 덮어쓰지 않는다.
4. 기존 `~/.grok/config.toml`의 이 플러그인 전용 `[skills].paths` 항목과 `~/.grok/skills/han-*`·`chef-*` 링크 정보를 먼저 백업한다. 이전 이름(`han`)으로 등록한 `~/.grok/plugins/han`도 함께 확인한다. native 플러그인을 등록하기 전에 그 항목과 링크만 제거해 중복 발견을 막는다. 다른 스킬 경로와 링크는 보존한다.
5. `grok inspect`에서 native chef가 `<repo>/plugins/chef`를 선택하고 스킬 8개·훅 1개와 native 역할 6개를 읽는지 확인한다. `agents`의 실제 역할 이름·원본 경로를 확인하며 `provides.agents`의 등록 경로 수를 역할 수로 보지 않는다. `chef-watch`와 `chef-handoff`는 없어야 한다. 단순 `grok plugin install`은 성공해도 compat chef가 선택될 수 있으므로 inspect 결과로 적용 여부를 판단한다.
6. Grok Bot 스킬 라이브러리에도 아래 여덟 폴더를 등록하거나, 저장소 경로의 파일을 읽게 한다.
   - `plugins/chef/grok/skills/chef-status/SKILL.md`
   - `plugins/chef/grok/skills/chef-doctor/SKILL.md`
   - `plugins/chef/grok/skills/chef-mission-brief/SKILL.md`
   - `plugins/chef/grok/skills/chef-run/SKILL.md`
   - `plugins/chef/grok/skills/chef-mission/SKILL.md`
   - `plugins/chef/grok/skills/chef-slots/SKILL.md`
   - `plugins/chef/grok/skills/chef-adapter-init/SKILL.md`
   - `plugins/chef/grok/skills/chef-distill/SKILL.md`
   `chef-watch`와 `chef-handoff` 폴더는 이 등록 스킬을 가리키는 저장소 안내이다. 등록하지 않는다.
   스크립트는 복사본을 만들지 않는다. 조회는 저장소 안의 `han_state.py`를, 미션·슬롯·어댑터 점검·피드백 정리는 `plugins/chef/skills/`의 공유 스크립트를 절대 경로로 호출한다.
7. 코디네이터는 상태를 읽고 브리프를 만든다. 기본 인계는 메인 작업 폴더 `<project>` 또는
   슬롯 작업 폴더 `<slot-path>`의 실행 봇에 보낼 SendToAgent 블록이다.
   슬롯 봇은 미션이 붙기 전까지 대기한다. Claude·Codex 문장은 사용자가 그 런타임을 지정할 때만 붙인다.

확인:

```text
python plugins/chef/grok/scripts/han_state.py snapshot --project <프로젝트>
python plugins/chef/grok/scripts/han_state.py brief --project <프로젝트> --runtime grok --goal "한 줄 목표"
python -m pytest plugins/chef/grok/test_han_state.py -q -p no:cacheprovider
```

역할 경계와 호출 예는 [plugins/chef/grok/README.md](plugins/chef/grok/README.md)에 있다.

## 설치

필요 조건: Windows, Windows Terminal(`wt.exe`), Python 3, Perforce CLI(`p4`), Unity(슬롯 임포트용),
어댑터가 부르는 프로젝트 도구. 예시의 `Tool/VcsWrapper` 등은 가상의 이름이며 이 플러그인에 포함되지 않는다.
`<repo>`는 이 저장소를 받은 폴더이고 `<project>`는 작업할 프로젝트 폴더다.
개인 규칙(`inject_rules`)은 작성자 취향이라 보통 켜지 않는다. 취향 규칙 없이 문서 규칙·메인 세션 규칙·사람용 문서 자료 안내만 받으려면 `shared`를 쓴다.

1. 이 저장소를 받는다(예: `D:/Work/chef`).
2. Python 3이 PATH에 있는지 본다: `python <repo>/plugins/chef/skills/adapter-init/scripts/check_guard.py --doctor`.
3. 마켓플레이스 등록과 설치:
   ```
   claude plugin marketplace add <repo>
   claude plugin install chef@chef --scope user --config adapters_dir=<repo>/adapters
   ```
   개인 규칙을 쓰려면 `--config inject_rules=on`을, `personal-rules.md`만 빼려면 `--config inject_rules=shared`를 더한다.
   설치본은 플러그인 캐시에서 돌아서 플러그인 설정 `adapters_dir`(이 저장소의 `adapters` 폴더 경로)이 없으면
   가드가 어댑터를 못 찾는다. `install`이 이 값을 묻지 않을 수 있으니 설치할 때 같이 주거나 설치 뒤에 확인·저장한다.
   ```
   claude plugin configure chef@chef --json
   echo '{"adapters_dir": "<repo>/adapters", "inject_rules": "on"}' | claude plugin configure chef@chef --values-stdin
   ```
   `configure --json`은 옵션과 아직 안 정한 것을 보여 주고, `--values-stdin`은 stdin의 JSON 값을 저장한다.
   `/plugin` 화면의 설정에서 넣어도 된다. 저장된 값은 `~/.claude/settings.json`의
   `pluginConfigs["chef@chef"].options.adapters_dir`에서 확인한다(공식 문서의 저장 위치).
   세션 시작 훅이 이 값을 환경변수 `HAN_ADAPTERS_DIR`로 Bash 도구에 넘겨, 스킬·스크립트가 같은 레지스트리를 쓴다.
4. 내 프로젝트용 어댑터를 만든다. `adapters/example/`을 `adapters/<내 프로젝트>/`로 복사해 고치거나
   `/chef:adapter-init`으로 초안을 만든다.
   - `adapter.json` — 프로젝트 이름, 단계, 가드 규칙, 슬롯 준비 단계의 명령·경로를 내 프로젝트에 맞게 고친다.
   - `adapter.md` — 각 절을 내 프로젝트의 절차로 채운다.
   - `guard-cases.json` — 가드가 막을 것·허용할 것의 회귀 사례. 바꾼 규칙에 맞춰 고치고
     `python plugins/chef/skills/adapter-init/scripts/check_guard.py adapters/<내 프로젝트>/adapter.json`이 `mismatches=0`인지 본다.
5. PC별 파일을 만든다(git에 안 올라감).
   - `adapters/projects.json` — `adapters/projects.example.json`을 복사해 `adapter`를 내 어댑터 이름으로, `roots`를 내 프로젝트 폴더와 슬롯 폴더 glob으로 고친다.
   - `adapters/<내 프로젝트>/local.json` — `local.example.json`을 복사해 `slots.root`·`slots.client`·`slots.mainClient`·`vars.unityExe`·`vars.main`을
     내 경로와 작업 공간 이름(`ws-<이름>_{name}` 형태)으로 고친다.
6. 기존 전역 `~/.claude/CLAUDE.md`·`~/.claude/agents/`에 같은 내용이 있으면 중복 로드되니 지우거나,
   `inject_rules`를 비우거나 환경변수 `HAN_RULES_DISABLE=1`로 규칙 주입을 끈다(`HAN_RULES_DISABLE`이 `inject_rules`보다 우선).
7. 슬롯을 쓰면 새 세션에서 `/chef:slots`로 `create`한다.
8. 새 세션에서 확인: `/chef:mission`·`/chef:slots status`가 보이고 동작하는지, 에이전트 목록에 `chef:researcher`가 있는지, 규칙을 켰다면 규칙이 들어왔는지.

업데이트는 저장소를 갱신하고 `claude plugin marketplace update chef`와 `/reload-plugins`를 한다.
Grok은 native `~/.grok/plugins/chef`의 대상과 `grok inspect`가 선택한 경로·manifest를 push 커밋과 대조한다. 이전 이름(`han`)의 등록과 이 플러그인 전용 skills 경로·링크는 백업한 뒤 등록 해제하고, 다른 스킬 설정은 보존한다. 실행 중인 Grok 세션은 다시 연다.
`projects.json`·`local.json`은 git에 없어서 덮이지 않는다.

## 고친 뒤

- Claude 메인 슬롯 새 세션·재개는 기본 500000토큰, Codex는 200000토큰에서 압축한다. `slots.autocompact`로 100000~1000000 사이에서 바꾼다.
  이미 실행 중인 세션은 Claude의 `/autocompact 500k` 또는 다음 슬롯 재개 때 적용한다.
- 구현 역할은 `dev-small`(승인된 문서·정해진 소규모 수정)과 `dev-medium`(복잡한 구현, high effort)이다.
  `dev-small`은 Claude Sonnet 5.5 / medium, Codex GPT-6 Luna / high로 고정한다.
  Claude `dev-medium`은 기존 Sonnet / high, `researcher`와 `reviewer-small`은 기존 Sonnet / medium을 쓴다.
  첫 심층 리뷰·기록 감사의 기본 역할 `chef:code-reviewer`는 Opus / high이며 프로젝트 리뷰어(예: `code-reviewer`)가 없을 때 쓴다. 좁은 재리뷰와 뜻·승인 범위가 그대로인 작은 문서 변경(새 규칙·규칙 관계 변경과 질문·보고 문서는 제외)의 첫 리뷰는 세 런타임 모두 작은 리뷰어가 맡는다. Claude `chef:reviewer-small`(Sonnet / medium), Codex `chef:reviewer-small`(GPT-6.1 Terra / high), Grok `chef:reviewer-small`(모델 상속 / medium)이며 첫 리뷰 브리프에는 변경 전 본 대비 diff를 넣는다. 작은 리뷰어가 뜻·승인 범위 변경이나 새 규칙을 발견하면 clean을 선언하지 않고 심층 리뷰로 확대한다. Codex `chef:code-reviewer`는 모델을 고정하지 않고 부모를 상속한다.
  Codex `dev-medium`은 GPT-6.1 Sol / high로 고정한다.
  코디네이터가 직접 편집하지 않는 미션 경계와 독립 리뷰는 유지한다.
- 테스트·Unity Play 실행은 `roles.verify` 하나가 맡는다. Claude는 `chef:verifier`(Sonnet 5.5 / low),
  Codex는 `chef:verifier`(GPT-6 Luna / high)로 고정한다. 구현자는 공유 Unity 검증을 직접 실행하지 않는다.
  검증자는 코드·검증 범위·모델을 바꾸지 않고 실패·예상 밖 상태·미확인 증거를 메인에 반환한다.
  공유 Unity 순서와 에디터 수명, 최종 완료 판정은 코디네이터가 맡고 독립 리뷰는 별도로 받는다.
- 어댑터 `rolePolicy`(`{"model": "definition"|"inherit", "subagents": true|false}`)는 mission 스킬이 역할을 파견할 때만 적용한다. 없으면 `definition`·`true`로 위 고정을 따른다. `inherit`면 Claude는 코디네이터 모델 별칭을 Agent `model` 인자로 넘기고 Codex는 등록된 역할의 TOML 모델이 적용되어 부모 상속을 지킬 수 없으므로 그 사실을 보고한다(위 고정은 정의의 값이 그대로 적용됨). `subagents: false`면 `dev-medium`류 브리프에 하위 파견 금지·분할안 반환을 넣는다(도구 차단 아님). Grok은 이미 충족된다. Claude는 최상위, Codex·Grok은 `runtimes.<런타임>.rolePolicy`에 둔다.
- 승인된 계약과 파일 소유가 독립적인 구현은 같은 턴에 파견한다. 관련 하위 작업까지 반환을 모으고 필요한
  `.meta`·생성물·CL 정리를 마친 뒤 검증 입력을 고정한다. 검증 자원이 다르면 단위마다 검증하고, 공유 Unity는 관련 구현이 돌아온 뒤 한 명만 실행한다. 검증 중 그 입력을 바꾸지 않는다.
  브리프에는 해당 작업의 파일·CL 소유, 기능 완료 범위와 반례를 넣는다. 같은 CL에 속해도 마일스톤별 검증 기록은 따로 유지한다.
- 미션 재개는 `context`, 파견 입력은 `reference snapshot`·`brief`, 검증 전에는 읽기 전용 `preflight --plan`을 쓴다.
  실제 검증은 `verify --plan`, 반환 근거는 `handoff`, 독립 리뷰 근거 등록 후 완료는 `complete` 한 호출로 연결한다.
  리뷰 파견 직전에는 `review-start --plan`으로 입력을 기록하고 후속 범위는 `review-scope`의 파일별 diff로 본다.
  Low만 남으면 코디네이터가 diff를 확인해 `review --status closed-low`로 닫으며 `complete`는 clean 또는 closed-low를 통과로 인정한다.
  [검증 계획과 결과 재사용](plugins/chef/skills/mission/references/verification-workflow.md)에 계약·예시가 있다.
  계획·receipt는 공통 `mission_state.py inspect --plan <경로>` 또는 `inspect --receipt <경로>`로 먼저 요약해 본다. 이는 기록 조회이며 현재 유효성이나 재사용 여부는 기존 `preflight`·`verify`가 판단한다.
  기존 ledger 이력은 유지하며 새 완료는 문자열 pass 대신 유효한 검증·리뷰 기록이 필요하다.

- 로컬 디렉터리 마켓플레이스라도 설치본은 캐시 복사본이다. 고친 뒤 `claude plugin marketplace update chef`와
  플러그인 업데이트(또는 `/reload-plugins`)로 반영한다. 버전을 올리면 확실히 갱신된다(`plugin.json`·`marketplace.json`).
  Grok은 native `~/.grok/plugins/chef`의 대상과 `grok inspect` 결과로 반영을 확인한다.
- 가드 테스트: `python -m pytest plugins/chef/hooks -q -p no:cacheprovider`
- 어댑터 회귀: `python plugins/chef/skills/adapter-init/scripts/check_guard.py <어댑터 이름>`
- 슬롯 테스트: `python -m pytest plugins/chef/skills/slots/scripts -q -p no:cacheprovider`
- Codex 런타임 테스트: `python -m pytest plugins/chef/codex/test_codex.py -q -p no:cacheprovider`
- Grok 조회 테스트: `python -m pytest plugins/chef/grok/test_han_state.py -q -p no:cacheprovider`
- 공통 스킬 절차: `plugins/chef/shared/skills/*.md`가 단일 정본이며 Claude·Codex·Grok의 `SKILL.md`는 공통 문서를 읽고 런타임 실행 차이만 적용한다. 공통 참조·양식은 `plugins/chef/skills/mission/references`에 한 벌만 둔다. Codex 기존 참조 경로는 정본 링크 포인터다. `python plugins/chef/codex/sync_shared.py`로 진입점·정본·포인터를 검사하고, `--write`는 참조 포인터를 갱신한다.
  검사는 런타임 참조 폴더의 불필요한 사본과 공백을 정규화한 120자 이상 비코드 문단의 재복제를 잡는다. 의역이나 짧은 구절의 의미 중복까지 판정하지 않는다.
- 설치·어댑터 점검: `python plugins/chef/skills/doctor/scripts/doctor.py`

사용량과 반복 리뷰는 [사용량 운영 규약](plugins/chef/skills/mission/references/usage-policy.md)을 따른다.
`usage_report.py --project 이름=경로 --start ISO시각 --end ISO시각 --output 결과.json`으로
Claude 입력·캐시·출력·Advisor와 리뷰 기록을 함께 집계한다. `--project`는 반복 지정한다.
작업 종류·단위·단계는 명시적인 활동 매핑으로 연결하고, 없는 기록이나 충돌은 미확인으로 남긴다.
검증 보고는 실제 실행과 단계 재사용, 실패 원인과 반복 실패를 구분한다. 설정과 예시는 위 운영 규약을 따른다.

참조 묶음·파견·반환 CLI와 첫 리뷰·후속 범위는 위 [사용량 운영 규약](plugins/chef/skills/mission/references/usage-policy.md)에 있다.
자동 검사는 원문 보존·입력 일치·실행 준비를 확인하며 승인 범위와 의미·독립 clean 판정은 사람이 확인한다.

## 설계 메모

- 서브에이전트는 부모 세션의 MCP 연결(Unity 에디터)을 같이 쓴다. 한 세션 안에서 독립적인 파일 구현은
  병렬로 진행하고 공유 에디터 조작은 순차로 실행한다. 별도 에디터가 필요한 병렬 미션은 슬롯을 쓴다.
  미션은 작업 공간 하나에 하나만 진행한다.
- Unity 에디터는 검증·에셋 임포트에 필요할 때만 연다. 슬롯 미션 시작·재개는 `--editor` 없이 실행하고,
  코디네이터가 `adapter.md` 절차로 필요 시 실행·준비 확인·결과 수집·정상 종료를 맡는다.
  이번 작업에서 연 인스턴스만 종료하며, 이미 열린 에디터는 재사용 후 유지한다.
- 가드는 명령 문자열 검사라 걸림줄일 뿐이다. 규칙별 예외는 같은 명령 조각 전체가 맞을 때만 적용한다.
  명시된 읽기 전용 판별·리터럴 검색 허용이 함께 실행하는 변경 명령의 허용은 아니다. 실제 통제는 미션 스킬의 멈춤 규칙이다.
- 마일스톤을 1시간 안으로 자르는 건 METR 작업 길이(현재 모델이 80% 확률로 끝내는 길이)에 맞춘 값이다.
- 기각한 대안
  - 상시 에이전트 군집(Gas Town류): 작성자 스스로 권하지 않고 비용이 단일 세션의 수 배다.
  - 클라우드 에이전트(Routines, Codex cloud, Copilot agent): GitHub 저장소가 전제라 Perforce 프로젝트에 못 쓴다.
  - 어댑터를 프로젝트 저장소에: 팀원은 플러그인이 없어 못 쓰고, 개인 운용 도구다.
  - Unity CLI 전면 전환: 공유 에디터 대기열·콘솔 읽기·프로젝트 전용 도구가 없고 아직 베타다. Pipeline 업그레이드 뒤 병행 비교한다.
- Grok 코디네이터는 상태를 읽고 메인·슬롯의 실행 봇에 작업을 넘길 수 있다. 로컬 상태·슬롯 조회는 chef 스크립트로 하고,
  저장소 코드 작업은 프로젝트의 실행 규칙을 따른다. Claude·Codex는 선택 경로다. `chef-status`와 `chef-doctor`는 읽기 전용이고, 인계는 `chef-mission-brief`, 구현 절차는 `chef-run`이다.
  미션·슬롯·어댑터 초안·피드백 정리는 `chef-mission`·`chef-slots`·`chef-adapter-init`·`chef-distill`이 공유 스크립트를 부른다.
  Main·slotN의 Grok CLI는 native 역할 여섯 개를 조율하며, depth 1에서 추가 분할은 메인이 맡는다. `slots.py run --runtime grok`은 ready·ACTIVE·소유권 조건을 확인한다. 대시보드는 `--runtime grok`을 지원하고, distill은 ACP `updates.jsonl`을 실제 경로를 지정해 읽으며 로그 기본값은 `~/.grok/han/distill/log.tsv`다. 등록은 그 여덟 스킬이며 `chef-watch`·`chef-handoff`는 등록하지 않는다.
  Grok CLI native chef는 `<repo>/plugins/chef`에서 읽는다. 이전 이름(`han`)의 등록과 이 플러그인 전용 skills 경로·링크는 중복 발견 여부를 확인해 정리하고, 전역 규칙은 `~/.grok/rules/`에 둔다. 읽기 전용 셸 목록은 `grok/README.md`.
