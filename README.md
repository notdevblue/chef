# chef

Claude Code · Codex CLI · Grok CLI · Hermes Agent에서 개발 작업을 계획하고, 에이전트에게 조사·구현·검증·리뷰를 맡기는 플러그인입니다.
중요한 계획과 결과는 사용자 승인을 받으며, 진행 상태를 기록해 이어서 작업할 수 있습니다.

- **미션**: 작업을 마일스톤으로 나눠 구현·검증·리뷰를 진행합니다.
- **슬롯**: 독립된 작업 공간에서 여러 작업을 병렬로 진행합니다.
- **실행 관리**: 실행 통계, 작업 대기열, 모델 선택 추천을 제공합니다.

## 설치

Python 3.11 이상과 사용할 런타임의 CLI가 필요합니다. macOS·Linux에서는 `python3` 또는 `python`을, Windows에서는 `python`을 PATH에 둡니다. [설치 안내](docs/install.md#python-3-진입점)

```bash
git clone https://github.com/notdevblue/chef.git
cd chef
```

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

처음 설치할 때 체크아웃의 `plugins/chef`를 `~/.grok/plugins/chef`에 연결합니다.
Linux/macOS에서는 저장소 폴더에서 실행합니다.

```bash
mkdir -p ~/.grok/plugins
ln -s "$PWD/plugins/chef" ~/.grok/plugins/chef
grok inspect
```

Windows에서는 junction을 사용합니다. 기존 설치가 있다면 [설치·업데이트 안내](docs/install.md)를 따르세요.

### Hermes Agent (macOS·Linux)

Hermes native 플러그인은 실행 중인 Hermes 작업 세션에서 슬롯을 선택합니다. GUI 터미널이나 새 Hermes 프로세스를
열지 않습니다. 기본 역할 파견은 현재 Hermes 프로필의 모델과 인증을 사용하며 여섯 Chef 역할을 지원합니다.
Codex CLI, Claude Code, 또는 사용자가 구성한 JSONL 브리지를 선택할 수도 있습니다. 설치와 제한 사항은
[Hermes 안내](plugins/chef/hermes/README.md)를 따르세요.

## 시작하기

프로젝트의 경로·도구·검증 방법을 담는 **어댑터**를 만든 뒤 미션을 시작합니다.

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
- [실행 통계·작업 대기열·모델 선택 추천](docs/autonomous-lab.md)
