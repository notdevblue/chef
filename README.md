# chef

Claude Code · Codex CLI · Grok CLI에서 개발 작업을 계획하고, 에이전트에게 조사·구현·검증·리뷰를 맡기는 플러그인입니다.
중요한 계획과 결과는 사용자 승인을 받으며, 진행 상태를 기록해 이어서 작업할 수 있습니다.

- **미션**: 작업을 마일스톤으로 나눠 구현·검증·리뷰를 진행합니다.
- **슬롯**: 독립된 작업 공간에서 여러 작업을 병렬로 진행합니다.
- **실행 관리**: 실행 통계, 작업 대기열, 모델 선택 추천을 제공합니다.

## 설치

Python 3.11 이상과 사용할 런타임의 CLI가 필요합니다.

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

## 시작하기

프로젝트의 경로·도구·검증 방법을 담는 **어댑터**를 만든 뒤 미션을 시작합니다.

| 런타임 | 어댑터 만들기 | 작업 시작 |
| --- | --- | --- |
| Codex | `$chef:chef-adapter-init` | `$chef:chef-mission <작업>` |
| Claude Code | `/chef:adapter-init` | `/chef:mission <작업>` |
| Grok | `chef-adapter-init` | `chef-run <작업>` |

개인 어댑터 폴더는 `HAN_ADAPTERS_DIR`로 지정합니다. Codex의 역할 등록과 훅 신뢰를 포함한
프로젝트 연결 절차는 [설치 안내](docs/install.md#프로젝트-연결)에 있습니다.
슬롯에서 새 CLI 세션을 여는 기능은 Windows Terminal이 필요합니다.

## 문서

- [설치·설정·업데이트](docs/install.md)
- [개발·검증](docs/development.md)
- [실행 통계·작업 대기열·모델 선택 추천](docs/autonomous-lab.md)
