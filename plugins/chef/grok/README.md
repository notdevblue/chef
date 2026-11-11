# Grok Bot·CLI에서 chef 보기

Grok 코디네이터는 바깥에서 상태를 보고, 메인 또는 슬롯의 실행 봇에 작업을 넘긴다. 실행은 Grok만으로 된다.
로컬 상태·슬롯 조회는 chef 스크립트로 하고, 저장소 코드 작업은 프로젝트의 실행 규칙을 따른다.
Claude와 Codex는 선택인 안쪽 세션이다. 없어도 미션을 진행할 수 있다.

조회 스킬은 읽기만 한다. 봇을 깨우는 인계는 `chef-mission-brief`다. 실행 절차는 `chef-run`이고, Bot과 CLI가 같이 쓴다.
Main 또는 slotN의 Grok CLI는 오케스트레이터다. 조사·구현·검증·리뷰·감사를 native 역할에 맡기고 결과를 통합한다.

프로젝트 규칙은 작업 폴더의 `AGENTS.md`·`CLAUDE.md`와 `.claude/rules/`다. Grok CLI는 이를 이미 읽는다.
전역 실행 규칙은 `~/.grok/rules/`에 두며, CLI가 읽은 규칙은 `grok inspect`에서 확인한다. `chef-run`은 그 규칙을 복제하지 않는다.

## 실행 역할

| 자리 | 하는 일 | 경로 |
| --- | --- | --- |
| 바깥 코디네이터 | 상태·브리프·미션 대기열을 보고 실행 봇에 인계 | — |
| 메인, 슬롯이 아닌 작업 | 메인 작업 폴더에서 역할별 에이전트 파견·통합 | `<project>` |
| 선택한 슬롯 | 슬롯 작업 폴더에서 역할별 에이전트 파견·통합 | `<slot-path>` |

슬롯 봇은 미션이 붙기 전까지 대기한다. 새 미션은 비어 있는 ready 슬롯에만 붙인다.
메인과 슬롯이 아닌 작업은 메인 작업 폴더의 실행 봇에게 보낸다.

native 역할은 아래 여섯 개다. 모두 부모의 Grok 4.7 모델을 상속하며, effort는 역할 정의에 고정한다. 리뷰와 기록 감사는 같은 `chef:reviewer`를 각각 새 작업으로 파견하고, 좁은 재리뷰와 뜻·승인 범위가 그대로인 작은 문서 변경(새 규칙·규칙 관계 변경과 질문·보고 문서는 제외)의 첫 리뷰는 `chef:reviewer-small`이 맡는다.

| 역할 | native 이름 | effort |
| --- | --- | --- |
| 조사 | `chef:researcher` | medium |
| 작은 구현 | `chef:dev-small` | medium |
| 여러 파일·계층 구현 | `chef:dev-medium` | high |
| 검증 | `chef:verifier` | low |
| 리뷰·기록 감사 | `chef:reviewer` | high |
| 좁은 재리뷰·작은 문서 변경의 첫 리뷰 | `chef:reviewer-small` | medium |

chef는 Grok 기본 depth 1에 맞춰 메인이 추가 파견을 맡는다. 하위 에이전트가 추가 분할을 요구하면 소유 파일·계약·검증 범위를 메인에 돌려주고, 메인이 같은 역할의 새 하위 에이전트를 띄운다. 전역 깊이 설정은 바꾸지 않는다. 어댑터 `rolePolicy`(`runtimes.grok.rolePolicy`)는 이미 충족돼 추가 동작이 없다. 작업 단계와 사람 확인 A·B·C는 공유 미션 절차를 따른다.

## Grok CLI에 native chef 등록

Grok CLI는 저장소의 `plugins/chef/.grok-plugin/plugin.json` manifest를 사용한다. `claude plugin`과 `codex plugin`으로 Grok 스킬을 넣지 않는다.
native chef는 아래 여덟 스킬을 등록한다. `chef-status`·`chef-doctor`·`chef-mission-brief`의 역할은 그대로다. 실행 절차는 `chef-run`이다.
미션 상태 쓰기는 `chef-mission`, 슬롯 조작은 `chef-slots`, 어댑터 초안은 `chef-adapter-init`, 피드백 정리는 `chef-distill`이다.

1. 이 저장소를 Grok 셸이 닿는 PC의 `<repo>`에 받는다.
2. 그 셸에 레지스트리를 알린다. `HAN_ADAPTERS_DIR=<저장소>/adapters` 또는 `HAN_REPO=<저장소>`.
   프로젝트의 `.grok/orchestrator/adapter.json`이 있으면 그게 먼저다. 없으면 같은 자리의 `.claude`, 그다음 `.codex` 어댑터를 읽는다.
3. `~/.grok/plugins/chef`를 `<repo>/plugins/chef`으로 연결한다. Windows에서는 junction, POSIX에서는 symlink를 쓴다. 이미 그 경로가 있으면 대상을 확인하고 덮어쓰지 않는다.
4. 기존 `~/.grok/config.toml`의 이 플러그인 전용 `[skills].paths` 항목과 `~/.grok/skills/han-*`·`chef-*` 링크 정보를 먼저 백업한다. 이전 이름(`han`)으로 등록한 `~/.grok/plugins/han`도 함께 확인한다. native chef를 등록하기 전에 그 항목과 링크만 제거해 중복 발견을 막는다. 다른 스킬 경로와 링크는 보존한다.
5. `grok inspect`에서 native chef의 선택 경로가 `<repo>/plugins/chef`인지, 스킬 8개·훅 1개와 위 native 역할 6개인지 확인한다. `agents`의 실제 이름·원본 경로를 확인한다. `provides.agents`는 등록 경로 수라서 역할 수와 다를 수 있다. inspect는 모델·effort 실행 증거를 제공하지 않으므로 역할 파일과 실제 child 기록으로 따로 확인한다. `chef-watch`와 `chef-handoff`는 없어야 한다. 단순 `grok plugin install`은 성공해도 compat chef가 선택될 수 있으므로 inspect 결과로 적용 여부를 판단한다.
6. Grok Bot 스킬 라이브러리에도 그 여덟 폴더만 등록하거나, 저장소 파일을 직접 읽게 한다.
   - `plugins/chef/grok/skills/chef-status/SKILL.md`
   - `plugins/chef/grok/skills/chef-doctor/SKILL.md`
   - `plugins/chef/grok/skills/chef-mission-brief/SKILL.md`
   - `plugins/chef/grok/skills/chef-run/SKILL.md`
   - `plugins/chef/grok/skills/chef-mission/SKILL.md`
   - `plugins/chef/grok/skills/chef-slots/SKILL.md`
   - `plugins/chef/grok/skills/chef-adapter-init/SKILL.md`
   - `plugins/chef/grok/skills/chef-distill/SKILL.md`
7. `plugins/chef/grok/skills/chef-watch/`와 `plugins/chef/grok/skills/chef-handoff/`는 위 등록 스킬을 가리키는 저장소 안내이다. 등록하지 않는다.
   `chef-handoff`는 메인·슬롯의 실행 봇을 깨우는 흐름을 적는다.
8. 스크립트는 복사하지 않는다. 조회는 `<저장소>/plugins/chef/grok/scripts/han_state.py`를, 미션·슬롯·어댑터 점검·피드백 정리는 `<저장소>/plugins/chef/skills/` 아래 공유 스크립트를 절대 경로로 부른다.

## 직접 확인

```text
python plugins/chef/grok/scripts/han_state.py snapshot --project <프로젝트>
python plugins/chef/grok/scripts/han_state.py brief --project <프로젝트> --runtime grok --goal "한 줄 목표"
python plugins/chef/grok/scripts/han_state.py brief --project <프로젝트> --runtime grok --slot slot1 --goal "한 줄 목표"
python plugins/chef/grok/scripts/han_state.py brief --project <프로젝트> --runtime claude --goal "한 줄 목표"
python plugins/chef/skills/doctor/scripts/doctor.py --runtime grok
python plugins/chef/skills/slots/scripts/dashboard.py --runtime grok --project <프로젝트>
python plugins/chef/skills/distill/scripts/distill.py --runtime grok extract --transcript <실제-대화-기록-경로> --out <저장소-밖-비공개-경로>/distill.md
python -m pytest plugins/chef/grok/test_han_state.py -q -p no:cacheprovider
```

`doctor.py --runtime grok`는 읽기 전용이다. 어댑터를 못 찾는 진단의 정본은 `chef-doctor`의 snapshot이다.
doctor는 native 역할의 이름·원본 경로를 확인하고 누락·다른 경로의 같은 이름을 경고한다. 검색 성공은 실제 파견·모델·effort 실행 검증과 별개다.

`han_state.py`의 `--runtime`을 비우면 `grok`다. `snapshot`은 파일을 만들지 않는다.
`brief --runtime grok`는 슬래시 명령 없이 SendToAgent 블록을 출력한다. 블록에는 실행 대상, 목표, 프로젝트 경로, 제약, 쓰기 확인, 코드 작업의 실행 규칙이 있다.
`claude`와 `codex`는 사람이 그 세션에 붙일 문장만 출력한다. Codex는 줄여 둔 선택 경로다.
대시보드는 `--runtime grok`으로 로컬 서버를 띄운다. distill은 지정한 ACP `updates.jsonl` 또는 다른 실제 기록 경로만 읽으며 세션의 최신 파일을 자동으로 고르지 않는다. Grok 로그 기본 경로는 `~/.grok/han/distill/log.tsv`이고 `GROK_HOME`으로 기준 폴더를 바꿀 수 있다.

어댑터 순서는 `plugins/chef/skills/mission/references/adapter-schema.md`의 "Grok에서 상태 읽기"와 같다.
