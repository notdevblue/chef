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

native 역할은 아래 여섯 개다. 모두 부모의 Grok 4.7 모델을 상속하며, effort는 역할 정의에 고정한다. 리뷰와 기록 감사는 같은 `chef:code-reviewer`를 각각 새 작업으로 파견하고, 좁은 재리뷰와 [사용량 정책](../skills/mission/references/usage-policy.md)의 기준에 맞는 작은 문서 첫 리뷰는 `chef:reviewer-small`이 맡는다.

| 역할 | native 이름 | effort |
| --- | --- | --- |
| 조사 | `chef:researcher` | medium |
| 작은 구현 | `chef:dev-small` | medium |
| 여러 파일·계층 구현 | `chef:dev-medium` | high |
| 검증 | `chef:verifier` | low |
| 리뷰·기록 감사 | `chef:code-reviewer` | high |
| 좁은 재리뷰·작은 문서 변경의 첫 리뷰 | `chef:reviewer-small` | medium |

chef는 Grok 기본 depth 1에 맞춰 메인이 추가 파견을 맡는다. 하위 에이전트가 추가 분할을 요구하면 소유 파일·계약·검증 범위를 메인에 돌려주고, 메인이 같은 역할의 새 하위 에이전트를 띄운다. 전역 깊이 설정은 바꾸지 않는다. 어댑터 `rolePolicy`(`runtimes.grok.rolePolicy`)는 이미 충족돼 추가 동작이 없다. 작업 단계와 사람 확인 A·B·C는 공유 미션 절차를 따른다. 기본 `implement` 스킬과의 경계는 [chef 리뷰 계약](skills/chef-mission/references/grok-roles.md#chef-리뷰-계약)을 따른다.

## Grok CLI에 native chef 등록

[마켓플레이스 설치·전환 안내](https://github.com/notdevblue/chef/blob/main/docs/install.md#grok-등록)를 따른다.
`grok inspect --json`에서 실제 스킬·역할·훅 원본 경로를 확인한다.
파견 시 적용된 모델과 effort는 역할 파일과 실제 child 기록에서 확인한다.

native chef는 아래 여덟 스킬을 등록한다. Grok Bot 스킬 라이브러리에서도 아래 스킬을
등록하거나, 스크립트와 공통 문서를 포함한 플러그인 폴더에서 직접 읽게 한다.

- `grok/skills/chef-status/SKILL.md`
- `grok/skills/chef-doctor/SKILL.md`
- `grok/skills/chef-mission-brief/SKILL.md`
- `grok/skills/chef-run/SKILL.md`
- `grok/skills/chef-mission/SKILL.md`
- `grok/skills/chef-slots/SKILL.md`
- `grok/skills/chef-adapter-init/SKILL.md`
- `grok/skills/chef-distill/SKILL.md`

`chef-watch`와 `chef-handoff`는 등록 스킬을 가리키는 저장소 안내이며 등록하지 않는다.
개인 레지스트리는 `HAN_ADAPTERS_DIR` 또는 `HAN_REPO/adapters`로 찾는다.
프로젝트의 `.grok/orchestrator/adapter.json`이 있으면 그게 먼저다. 없으면 같은 자리의 `.claude`, 그다음 `.codex` 어댑터를 읽는다.

## 직접 확인

`<plugin-root>`는 `grok inspect --json`에서 활성 `chef` 플러그인의 `path`다.

```text
python <plugin-root>/grok/scripts/han_state.py snapshot --project <프로젝트>
python <plugin-root>/grok/scripts/han_state.py brief --project <프로젝트> --runtime grok --goal "한 줄 목표"
python <plugin-root>/grok/scripts/han_state.py brief --project <프로젝트> --runtime grok --slot slot1 --goal "한 줄 목표"
python <plugin-root>/grok/scripts/han_state.py brief --project <프로젝트> --runtime claude --goal "한 줄 목표"
python <plugin-root>/skills/doctor/scripts/doctor.py --runtime grok
python <plugin-root>/skills/slots/scripts/dashboard.py --runtime grok --project <프로젝트>
python <plugin-root>/skills/distill/scripts/distill.py --runtime grok extract --transcript <실제-대화-기록-경로> --out <저장소-밖-비공개-경로>/distill.md
python -m pytest tests/integration/grok/test_han_state.py -q -p no:cacheprovider
```

`doctor.py --runtime grok`는 읽기 전용이다. 어댑터를 못 찾는 진단의 정본은 `chef-doctor`의 snapshot이다.
doctor는 native 역할의 이름·원본 경로를 확인하고 누락·다른 경로의 같은 이름을 경고한다. 검색 성공은 실제 파견·모델·effort 실행 검증과 별개다.

`han_state.py`의 `--runtime`을 비우면 `grok`다. `snapshot`은 파일을 만들지 않는다.
`brief --runtime grok`는 슬래시 명령 없이 SendToAgent 블록을 출력한다. 블록에는 실행 대상, 목표, 프로젝트 경로, 제약, 쓰기 확인, 코드 작업의 실행 규칙이 있다.
`claude`와 `codex`는 사람이 그 세션에 붙일 문장만 출력한다. Codex는 줄여 둔 선택 경로다.
대시보드는 `--runtime grok`으로 로컬 서버를 띄운다. distill은 지정한 ACP `updates.jsonl` 또는 다른 실제 기록 경로만 읽으며 세션의 최신 파일을 자동으로 고르지 않는다. Grok 로그 기본 경로는 `~/.grok/han/distill/log.tsv`이고 `GROK_HOME`으로 기준 폴더를 바꿀 수 있다.

어댑터 순서는 `<plugin-root>/skills/mission/references/adapter-schema.md`의 "Grok에서 상태 읽기"와 같다.
