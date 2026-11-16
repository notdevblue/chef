# 설치·설정·업데이트

[README](../README.md)의 런타임별 설치를 마친 뒤 프로젝트를 연결합니다.
`<repo>`는 Chef 체크아웃의 절대 경로, `<project>`는 작업할 프로젝트 경로입니다.

## 필요 조건

- Python 3.11 이상, 사용할 Claude Code·Codex CLI·Grok CLI.
- 슬롯에서 새 CLI 세션을 열 때는 Windows와 Windows Terminal(`wt.exe`).
- 프로젝트 어댑터가 사용하는 도구. Perforce·Unity는 해당 프로젝트에서 사용하는 경우에만 필요합니다.

Python과 어댑터 환경은 다음 명령으로 점검합니다.

```text
python <repo>/plugins/chef/skills/adapter-init/scripts/check_guard.py --doctor
```

## 프로젝트 연결

1. 사용할 런타임에서 README의 어댑터 생성 스킬을 실행하거나,
   `adapters/example/`을 개인 어댑터 폴더에 복사합니다.
2. `adapter.json`에 프로젝트 경로·단계·가드·검증 방법을, `adapter.md`에 실행 절차를 설정합니다.
   [어댑터 계약](../plugins/chef/skills/mission/references/adapter-schema.md)을 참고합니다.
3. 개인 레지스트리를 쓰면 `adapters/projects.example.json`을 `adapters/projects.json`으로 복사해
   어댑터 이름과 프로젝트·슬롯 roots를 지정합니다. PC별 경로는 어댑터의 `local.json`에 둡니다.
4. 런타임을 시작할 셸에 `HAN_ADAPTERS_DIR=<repo>/adapters`를 설정합니다.
   로컬 프로젝트의 `.claude/orchestrator/adapter.json`·`.codex/orchestrator/adapter.json`·
   `.grok/orchestrator/adapter.json`을 사용하는 방식도 지원합니다. 발견 순서는 위 계약을 따릅니다.

PowerShell:

```powershell
$env:HAN_ADAPTERS_DIR = '<repo>/adapters'
codex -C <project>
```

Bash:

```bash
export HAN_ADAPTERS_DIR="<repo>/adapters"
cd <project>
codex
```

이 명령은 현재 셸에 적용됩니다. 새 셸에서도 쓰려면 환경변수를 셸 시작 설정에 저장합니다.
개인 어댑터와 `projects.json`·`local.json`은 공개 Git에 추가하지 않습니다.

## Codex 역할과 훅

새 세션의 `/hooks`에서 플러그인의 훅을 검토하고 신뢰합니다. hooks 기능이 꺼져 있으면
`codex --enable hooks`로 시작합니다. 훅이 바뀌면 다시 검토합니다.

미션의 역할 파견에는 `plugins/chef/codex/agents/`의 TOML 등록이 필요합니다.
등록 폴더는 `$CODEX_HOME/agents/`, `CODEX_HOME`이 없으면 `~/.codex/agents/`입니다.
플러그인 설치는 이 전역 역할 폴더를 자동으로 만들지 않습니다.
기존 파일을 먼저 비교한 뒤 Chef 역할 파일을 등록하고, 업데이트할 때도 사본을 함께 갱신합니다.
다른 사용자 파일이나 플러그인에 없는 옛 역할은 자동으로 지우지 않습니다.
[Codex 역할 계약](../plugins/chef/codex/skills/chef-mission/references/codex-roles.md)에서 이름과 모델 설정을 확인합니다.
슬롯별 등록은 어댑터의 `slots.codexAgentConfigs`로 지정할 수도 있습니다.

`HAN_ADAPTERS_DIR`을 Codex 설정에 저장하려면 기존 설정 파일의 아래 항목을 사용합니다.

```toml
[shell_environment_policy.set]
HAN_ADAPTERS_DIR = "<repo>/adapters"
```

설정 파일은 `$CODEX_HOME/config.toml`, `CODEX_HOME`이 없으면 `~/.codex/config.toml`입니다.
시작 셸의 환경변수가 우선하며 SessionStart 훅은 부모 셸을 바꾸지 않습니다.

## Claude Code 설정

Claude 플러그인 설정 `adapters_dir`에 개인 어댑터 폴더의 절대 경로를 저장합니다.
`/plugin` 화면에서 설정하거나 CLI를 사용합니다.

```text
claude plugin configure chef@chef --json
```

```bash
echo '{"adapters_dir": "<repo>/adapters"}' | claude plugin configure chef@chef --values-stdin
```

SessionStart 훅이 이 경로를 도구 환경의 `HAN_ADAPTERS_DIR`로 전달합니다.

## Grok 등록

[기존 Grok 등록 안내](../plugins/chef/grok/README.md#grok-cli에-native-chef-등록)를 따릅니다.
Windows에서는 junction, POSIX에서는 symlink로 `~/.grok/plugins/chef`를 체크아웃의 `plugins/chef`에 연결합니다.
기존 링크가 있으면 실제 대상을 확인하고 덮어쓰지 않습니다.

이전 `han` 등록이나 별도의 Chef skills 경로가 있으면 먼저 백업하고 해당 등록만 정리합니다.
`grok inspect`에서 native Chef의 선택 경로, 스킬 8개·훅 1개와 실제 역할 6개의 이름·원본 경로를 확인합니다.
`provides.agents`는 역할 수가 아닙니다. 실행 중인 Grok 세션은 새로 엽니다.

## 업데이트

Codex는 마켓플레이스를 먼저 갱신한 뒤 다시 설치합니다.

```bash
codex plugin marketplace upgrade chef-codex
codex plugin add chef@chef-codex
```

Claude Code:

```bash
claude plugin marketplace update chef
claude plugin update chef@chef
```

Grok은 체크아웃을 갱신하고 `grok inspect`로 선택된 native 경로를 확인합니다.
세 런타임 모두 새 세션에서 스킬과 훅을 확인합니다. 개인 어댑터는 설치 캐시 밖에 보관합니다.

로컬 경로를 마켓플레이스로 등록한 Codex 설치는 위 Git 마켓플레이스 갱신과 구분해야 합니다.
설정된 소스와 명령 문법은 `codex plugin marketplace list --json`과 각 명령의 `--help`로 확인합니다.

## 문제 확인

런타임의 `chef-doctor` 스킬 또는 공유 doctor를 사용합니다.

```text
cd <project>
python <repo>/plugins/chef/skills/doctor/scripts/doctor.py --runtime codex
```

미션을 시작했으면 셸 마커의 출력뿐 아니라 가드 안내와 ACTIVE의 미션·세션 일치도 확인합니다.
진행·재개·승인·공유·검증의 정본 절차는 [공통 미션 안내](../plugins/chef/shared/skills/mission.md)에 있습니다.
