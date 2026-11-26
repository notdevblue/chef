# chef

Claude Code · Codex CLI · Grok CLI · Hermes Agent에서 개발 작업을 계획하고, 에이전트에게 조사·구현·검증·리뷰를 맡기는 플러그인입니다.
중요한 계획과 결과는 사용자 승인을 받으며, 진행 상태를 기록해 이어서 작업할 수 있습니다.

- **미션**: 작업을 마일스톤으로 나눠 구현·검증·리뷰를 진행합니다.
- **슬롯**: 독립된 작업 공간에서 여러 작업을 병렬로 진행합니다.
- **실행 관리**: 실행 통계, 작업 대기열, 모델 선택 추천을 제공합니다.

## 설치

Python 3.11 이상과 사용할 런타임의 CLI가 필요합니다. macOS·Linux에서는 `python3` 또는 `python`을, Windows에서는 `python`을 PATH에 둡니다. [설치 안내](docs/install.md#python-3-진입점)

### Codex

```bash
codex plugin marketplace add https://github.com/notdevblue/chef.git
codex plugin add chef@chef-codex
```

### Claude Code

```bash
claude plugin marketplace add https://github.com/notdevblue/chef.git
claude plugin install chef@chef --scope user
```

### Grok

```bash
grok plugin marketplace add https://github.com/notdevblue/chef.git
grok plugin install chef@chef --trust
grok inspect
```

기존 심볼릭 링크나 junction을 사용했다면 [Grok 전환 절차](docs/install.md#grok-등록)를 따르세요.

### Hermes Agent (macOS·Linux)

Hermes native 플러그인은 실행 중인 Hermes 작업 세션에서 슬롯을 선택합니다. GUI 터미널이나 새 Hermes 프로세스를
열지 않습니다. 기본 역할 파견은 현재 Hermes 프로필의 모델과 인증을 사용하며 여섯 Chef 역할을 지원합니다.
Codex CLI, Claude Code, 또는 사용자가 구성한 JSONL 브리지를 선택할 수도 있습니다. 설치와 제한 사항은
[Hermes 안내](plugins/chef/hermes/README.md)를 따르세요.

## 시작하기

프로젝트의 경로·도구·검증 방법을 담는 **어댑터**를 만든 뒤 미션을 시작합니다.
Claude·Codex·Grok 미션은 구현·검증·리뷰 역할을 공통 워커 라우터가 도구 역량·작업 관계·검증된 품질·진행 중 작업량에 따라 자동 배정합니다. 사용자가 런타임을 직접 고르지 않아도 되며, 별도 동시 실행 상한은 없습니다. 같은 CLI가 선택되면 native 역할 도구를 쓰고, 다른 CLI가 선택되면 각자의 로그인 상태로 별도 워커를 실행합니다. [라우팅·요청·복구 계약](docs/cross-runtime-workers.md)을 참고하세요.

전역 설정 파일 `~/.chef/config.json`에서 `{"workers":{"mode":"native"}}`로 자동 배정을 끄고 현재 서비스만 사용할 수 있습니다. `"auto"`로 다시 켜며, `"claude"`·`"codex"`·`"grok"`으로 서비스를 고정할 수도 있습니다. 프로젝트의 `workers.mode`나 미션에서 “이번에는 Claude만 써”라는 요청, 실행별 `dispatch --mode native`로 덮어쓸 수 있습니다. [모드 설정·우선순위](plugins/chef/skills/mission/references/cross-runtime-workers.md#자동-배정-켜기끄기)

`workers.runtimes: ["claude", "codex"]`로 Grok을 제외하거나, `workers.roleRouting.code-reviewer.runtime: "claude"`와 `reviewer-small.runtime: "claude"`로 리뷰만 Claude에 고정할 수 있습니다. 고정한 서비스가 실행되지 않으면 다른 서비스로 넘기지 않습니다. [조합 설정 예시](plugins/chef/skills/mission/references/cross-runtime-workers.md#허용-서비스와-역할별-고정)

| 런타임 | 어댑터 만들기 | 작업 시작 |
| --- | --- | --- |
| Codex | `$chef:chef-adapter-init` | `$chef:chef-mission <작업>` |
| Claude Code | `/chef:adapter-init` | `/chef:mission <작업>` |
| Grok | `chef-adapter-init` | `chef-run <작업>` |
| Hermes | `skill_view(name="chef:adapter-init")` | `skill_view(name="chef:mission")` 후 현재 대화에서 작업 요청 |

개인 어댑터 폴더는 `HAN_ADAPTERS_DIR`로 지정합니다. Codex의 역할 등록과 훅 신뢰를 포함한
프로젝트 연결 절차는 [설치 안내](docs/install.md#프로젝트-연결)에 있습니다.
Claude·Codex·Grok 슬롯은 Windows에서 Windows Terminal, POSIX에서 현재 대화형 터미널을 기본 사용합니다.
별도 창은 `slots.terminal`로 macOS Terminal·GNOME Terminal·Konsole을, GUI 없는 실행은 tmux를 명시합니다.
Hermes는 macOS·Linux의 로컬 terminal backend에서 `chef_slot`으로 현재 작업 세션을 슬롯에 연결합니다.

## 문서

- [설치·설정·업데이트](docs/install.md)
- [개발·검증](docs/development.md)
- [개발 구조와 호환성 계약](docs/architecture.md)
- [Claude·Codex·Grok 역할 워커](docs/cross-runtime-workers.md)
- [실행 통계·작업 대기열·모델 선택 추천](docs/autonomous-lab.md)
