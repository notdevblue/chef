# 설치·설정·업데이트

[README](../README.md)의 런타임별 설치를 마친 뒤 프로젝트를 연결합니다.
`<repo>`는 Chef 체크아웃의 절대 경로, `<project>`는 작업할 프로젝트 경로입니다.
예제 어댑터나 개발 명령이 필요하면 [기준 저장소](https://github.com/notdevblue/chef)를 체크아웃합니다.

## 필요 조건

- Python 3.11 이상, 사용할 Claude Code·Codex CLI·Grok CLI 또는 Hermes Agent.
- Claude·Codex·Grok 슬롯 실행은 Windows에서 Windows Terminal(`wt.exe`), macOS·Linux에서 대화형 터미널 또는 설정한 터미널 백엔드.
- Hermes는 macOS·Linux의 native plugin API와 로컬 terminal backend가 필요합니다. 기본 역할 파견은 현재 Hermes 프로필의 모델과 인증을 사용하며 프로필에 `delegation.inherit_mcp_toolsets: false`와 leaf 설정을 둡니다. leaf는 `delegation.max_spawn_depth: 1` 또는 `delegation.orchestrator_enabled: false`로 설정합니다. 명시적인 Codex backend에는 Codex CLI의 ChatGPT 로그인과 신뢰한 Chef 훅이 필요합니다. Claude 및 JSONL bridge 설정은 [Hermes 안내](../plugins/chef/hermes/README.md)를 따릅니다.
- 프로젝트 어댑터가 사용하는 도구. Perforce·Unity는 해당 프로젝트에서 사용하는 경우에만 필요합니다.

Python과 어댑터 환경은 다음 명령으로 점검합니다.
예제의 `python`은 실행 환경에 따라 `python3`으로 바꿀 수 있습니다.

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
   `.grok/orchestrator/adapter.json`·`.hermes/orchestrator/adapter.json`을 사용하는 방식도 지원합니다. 발견 순서는 위 계약을 따릅니다.

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
예제의 프로젝트 준비·빌드 명령은 대상 OS에서 실행 가능한 명령으로 설정합니다.
Windows의 `.bat`, `cmd`, `robocopy` 명령을 macOS·Linux에서 자동 변환하지 않습니다.

### Python 3 진입점

macOS·Linux에서 Claude Code·Codex CLI·Grok CLI를 직접 시작하면 Chef 훅이 PATH의 `python3`, `python` 순으로 Python 3.11 이상을 찾습니다. Windows의 훅은 PATH의 `python`을 사용합니다.

도구 명령에도 두 Python 이름을 모두 제공하려면 `python3 <repo>/plugins/chef/runtime.py codex`로 시작할 수 있습니다. 마지막 인자는 `claude`, `grok`, `hermes`도 가능합니다. macOS·Linux에서 이 진입점은 현재 Python을 가리키는 `python3`과 `python`을 자식 PATH에 추가합니다. 슬롯 런처와 Hermes 역할 워커도 이 환경을 자동으로 전달합니다.

### 슬롯 터미널과 에디터

`slots.terminal`의 기본 `auto`는 Windows에서 `wt`, macOS·Linux에서 `foreground`입니다.
`foreground`는 현재 대화형 TTY를 사용하고 종료까지 기다립니다. 새 창이 필요하면 `macos-terminal`, `gnome-terminal`, `konsole`을 명시합니다.
GUI 없는 실행은 설치된 `tmux`를 선택하면 독립된 detached 세션을 만듭니다. 자동 대기열은 foreground와 Hermes 새 세션을 실행하지 않습니다.
필요한 CLI·터미널·GUI 세션을 작업 공간 갱신 전에 확인하며, macOS Terminal 제어는 OS의 Automation 권한이 필요할 수 있습니다.

에디터는 기존 `slots.editor` 또는 `slots.editorArgv` 배열 중 하나를 사용합니다. macOS `.app` 경로는 번들의 실제 실행 파일을 확인해 직접 실행하므로 `open`의 PID를 에디터 PID로 오인하지 않습니다.
개인 실행 경로는 `local.json`에 설정합니다. 새 POSIX 터미널은 자체 TERM·SHELL을 사용하고, 런처는 Python PATH·어댑터 레지스트리·런타임 홈과 P4 설정을 명시적으로 전달합니다. 인증정보는 각 CLI의 정상 인증 설정에서 관리하며 쉘 명령에 API 키를 복사하지 않습니다.

준비 단계와 `beforeRun`에는 기존 `run` 문자열 대신 `argv` 문자열 배열을 사용할 수 있습니다.
각 인자를 따로 확장하고 셸 없이 실행하므로 공백·한글 경로를 별도로 따옴표로 감쌀 필요가 없습니다.
한 단계에는 `run`과 `argv` 중 하나만 지정합니다. 셸 기능이 필요하면 셸 실행 파일을 명시합니다.

```json
{"id": "prepare", "argv": ["python", "tools/prepare.py", "--slot", "{slot}"], "cwd": "{main}"}
```

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

최초 설치는 [README의 Grok 명령](../README.md#grok)을 실행합니다. `grok plugin marketplace list`에
`chef`가 이미 공개 기준 저장소를 가리키면 바로 설치할 수 있습니다. Grok 스킬은 설치본에 포함된
스크립트를 실행합니다. 개인 레지스트리를 사용하면 시작 셸에 `HAN_ADAPTERS_DIR`을 설정합니다.
기존 `HAN_REPO`는 그 아래 `adapters/`를 찾는 데 사용할 수 있습니다.

기존 링크에서 전환할 때는 다음 순서로 진행합니다.

1. 마켓플레이스에서 `chef@chef`를 설치합니다.
2. `~/.grok/plugins/chef`가 기존 Chef 체크아웃을 가리키는 심볼릭 링크 또는 junction인지 확인한 뒤,
   링크 자체를 Grok 플러그인 디렉터리 밖으로 옮겨 보관합니다. 이전 이름인 `han` 등록이나 Chef 전용
   스킬 링크·`[skills].paths` 항목이 남아 있다면 해당 등록도 백업하고 정리합니다.
3. `grok inspect --json`에서 선택된 `chef` 경로가 마켓플레이스 설치본인지 확인합니다.
   스킬 8개·훅 파일 1개와 실제 역할 6개의 원본 경로가 그 설치본 아래에 있어야 합니다.
   실행 중인 세션은 새로 열고 프로젝트 폴더의 훅 신뢰 상태와 실행을 확인합니다.

되돌릴 때는 `grok plugin uninstall chef --confirm --keep-data`으로 마켓플레이스 설치를 제거하고 보관한 링크와
이전 Chef 전용 등록을 복원합니다. 새 세션에서 `grok inspect --json`으로 복원된 경로를 확인합니다.

## Hermes 연결

[Hermes native 플러그인 안내](../plugins/chef/hermes/README.md)에 따라 등록하고 Hermes 프로세스를 재시작합니다.
프로젝트·서비스 계정·게이트웨이별 값은 개인 설정에 둡니다. 플러그인은 외부 서비스 인증이나 메시지 대상을 저장하지 않습니다.

Hermes의 레지스트리는 `HAN_ADAPTERS_DIR`이 우선이며, 없으면 `$HERMES_HOME/chef/adapters`가 존재할 때 사용합니다.
`HERMES_HOME`이 없으면 `~/.hermes`입니다. 이후 체크아웃의 `adapters/`를 확인합니다.
프로젝트 폴더를 위로 탐색할 때 각 폴더에서 `.hermes` → `.claude` → `.codex` → `.grok`의
`orchestrator/adapter.json`을 확인합니다. 가까운 폴더가 우선입니다.
각 파일은 공통 → 해당 OS → Hermes → Hermes 안의 해당 OS 순서로 병합합니다.
`adapter.json`을 처리한 뒤 `local.json`에도 같은 순서를 적용합니다. 자세한 키는 [어댑터 계약](../plugins/chef/skills/mission/references/adapter-schema.md)을 따릅니다.

현재 Hermes 작업의 `chef_slot`으로 ready 슬롯을 선택하고, 해당 세션에서 미션을 시작합니다.
같은 Hermes 소유 세션의 진행 중 미션은 `resume`으로 연결하며 다른 세션의 ACTIVE는 인계하지 않습니다.
native `delegate_task` 대신 `chef_delegate`로 Chef 역할을 파견합니다.
역할 워커는 선택한 backend의 준비 조건을 확인합니다. 기본 `hermes`는 현재 Hermes 프로필의 모델·인증과 `delegation.inherit_mcp_toolsets: false` 및 leaf/toolset 설정을 사용합니다. `codex`는 Codex 로그인과 신뢰한 Chef 훅, `claude`는 지원 버전의 Claude CLI 로그인과 Chef 훅, 명명된 JSONL bridge는 명시적인 실행 파일 구성이 필요합니다. 조건이 부족하면 실행을 중단하고 설정을 안내합니다. 상세 조건은 [Hermes 안내](../plugins/chef/hermes/README.md)를 따릅니다.

Hermes의 네이티브 SQLite 대화 기록은 `distill`의 JSONL 입력으로 읽지 않습니다.
명시적으로 내보낸 호환 대화 기록만 지정하고, 사용량이 필요하면 관측한 명시적 메타데이터를 제공합니다.
대기열은 새 Hermes 세션을 자동 생성하지 않습니다. 질문 무결성의 `pre_verify`는 Hermes의 제한된 반복 확인이며
프로세스 종료를 무조건 막는 훅이 아닙니다. 실제 결과·질문·승인을 확인한 뒤 완료를 보고합니다.

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

Grok:

```bash
grok plugin marketplace update chef
grok plugin update chef
grok inspect
```

Claude·Codex·Grok은 새 세션에서 스킬과 훅을 확인합니다. Hermes도 체크아웃을 갱신한 뒤 프로세스를 재시작합니다.
Hermes의 Codex 역할 워커는 설치된 Chef 코드와 실행 중인 Hermes의 코드가 일치해야 합니다.
개인 어댑터는 설치 캐시 밖에 보관합니다.

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
