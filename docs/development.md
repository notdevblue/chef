# 개발·검증

개발·설치·업데이트의 기준 저장소는 `notdevblue/chef`입니다.
기여할 때는 [저장소 지침과 공개 경계](../AGENTS.md)를 따릅니다.
회사·고객 자료, 개인 어댑터, 원본 대화·로그와 비밀정보는 배포본에 넣지 않습니다.

## 코드 구조

| 구성 | 위치 |
| --- | --- |
| 런타임 공통 스킬 절차 | `plugins/chef/shared/skills/` |
| 공통 Python 구현 | `plugins/chef/chef/` |
| Claude 진입점·역할 | `plugins/chef/skills/`, `plugins/chef/agents/` |
| Codex 진입점·역할 | `plugins/chef/codex/` |
| Grok 진입점·역할 | `plugins/chef/grok/` |
| Hermes native 등록·스킬 | `plugins/chef/hermes/` |
| Hermes 도구 연결·역할 워커 | `plugins/chef/chef/runtimes/hermes/` |
| 가드·훅 구현 | `plugins/chef/chef/guards/`, `chef/runtimes/` |
| 미션·검증·실행 관리 구현 | `plugins/chef/chef/mission/`, `chef/verification/` |
| 슬롯·읽기 전용 대시보드 | `plugins/chef/chef/slots/`, `chef/dashboard/` |
| 개발용 테스트·가상 데이터 | `tests/` |
| 개발·구조 검사 도구 | `tools/` |

공통 계약은 공유 절차에서 관리하고 런타임 진입점은 도구·역할·경로 차이만 둡니다.
역할의 실제 모델·effort는 각 런타임 정의와 참조 문서를 확인합니다.
구조를 바꿀 때는 [개발 구조와 호환성 계약](architecture.md)을 따릅니다.
기존 `scripts/`와 훅 경로는 호환 진입점이며 기능 구현을 추가하지 않습니다.

## 여러 가드의 1회 승인 공유

동일한 도구 호출을 여러 독립 가드가 판정하면 어댑터의
`guard.allowOnceParticipants`에 `chef-mission`과 각 가드의 고정 이름을 등록할 수 있습니다.
이 설정이 없으면 기존 `allow-once` 소비 방식이 유지됩니다. 가드 이름은 도구 입력에서
받지 않고 신뢰하는 훅 코드에서 지정합니다.

각 참여자는 먼저 소유 세션·워커·질문 쓰기·검증 입력 보호와 명령 형식을 검사한 뒤,
`chef.guards.allow_once.consume_shared_allow_once`를 호출합니다. Chef가 자체 규칙에서
허용하는 명령도 참여하므로, 두 번째 가드의 규칙에만 걸리는 승인도 공유됩니다.
모든 참여자는 같은 런타임의 `load_adapter` 결과를 `shared_once_context(adapter)`에
넣고 그 반환값을 `config`로 전달해야 합니다. 추가 정책 파일도 권위 입력이면
`guard.allowOnceAuthorityFiles`에 절대 파일 경로를 최대 8개 등록합니다. 이 함수는
어댑터 전체와 해당 파일의 현재 SHA-256을 함께 묶습니다. 상대 경로·중복·누락 파일은
거부합니다.

승인은 양끝 공백을 제외한 정확한 한 줄의 단일 명령에만 적용됩니다.
PowerShell(`pwsh`, `powershell`)과 POSIX 셸(`bash`, `sh`, `zsh`, `dash`, `ksh`)
계열만 공유 승인을 지원하며, 명시한 셸과 도구의 계열이 다르면 거부합니다. 최초 가드는
해당 줄을 원자적으로 소모한 뒤 영수증을 기록하며, 나머지 승인 줄의 바이트는 보존합니다.
영수증은 미션·현재 소유 세션·진입 시각·런타임·native 도구 호출 ID에 결부됩니다.
실효 작업 폴더·셸 종류·명령·설정이 달라지거나 같은 참여자가 재사용하면 거부합니다.
`tool_use_id`, `tool_call_id`, `call_id`를 지원하며, 누락·빈 값·서로 다른 별칭 값으로는
승인을 사용할 수 없습니다. 참여자마다 같은 호출에서 한 번씩만 인정됩니다.

`False`는 일치하는 승인이나 영수증이 없다는 뜻입니다. `SharedAllowOnceError`는
안전하게 확인할 수 없다는 뜻이므로 호출자는 거부해야 합니다. 승인 소비 후 영수증
기록이 실패해도 승인을 복원하지 않습니다. 실제 명령 실패도 승인을 복원하지 않으며,
같은 호출 ID에 새 승인 줄을 추가해도 기존 영수증의 재사용 제한은 유지됩니다.

## 테스트

저장소 루트에서 실행합니다.

```bash
python -m pip install -r requirements-dev.txt
python tools/check_architecture.py
python tools/check_shared.py
python tools/generate_entrypoints.py
python tools/generate_claude_profiles.py
python -m pytest -q -p no:cacheprovider
```

변경 범위에 맞춰 각 디렉터리의 pytest를 실행할 수도 있습니다.
기존 회귀 검사는 `tests/integration/`의 기능별 폴더에 있습니다.
저장·구현 식별 검사는 `tests/unit/`, 복사한 설치 트리와 기존 명령의 검사는 `tests/contracts/`입니다.
Codex 전용 검사는 `tests/integration/codex`, Grok 전용 검사는 `tests/integration/grok`에 있습니다.
Hermes 연결·현재 세션·역할 워커 검사는 `tests/integration/hermes`에 있습니다.
전용 검사만 실행한 결과를 전체 런타임 검증으로 보고하지 않습니다.

어댑터의 가드 규칙을 고쳤으면 해당 어댑터도 확인합니다.

```text
python plugins/chef/skills/adapter-init/scripts/check_guard.py <어댑터 이름>
```

`tools/check_shared.py`는 공통 스킬·런타임 링크·참조 포인터를 검사합니다.
이 도구의 `--write`는 Codex 참조 포인터를 갱신합니다. 공통 절차의 사본을 런타임 폴더에 추가하지 않습니다.
`tools/generate_claude_profiles.py`는 Claude 장기 대기용 역할의 캐시 설정만 원본 역할에 덧붙입니다.
원본 역할을 바꿨으면 `--write`로 프로필을 다시 생성하며, 검사 모드는 다른 모델·effort·도구·본문으로의 drift를 거부합니다.
호환 스크립트의 정본 목록은 `tools/entrypoints.json`입니다. 경로·대상·기본 런타임을 바꿀 때는
이 목록을 수정하고 `python tools/generate_entrypoints.py --write`로 생성합니다.
인자 없이 실행하면 생성 결과와 실제 파일의 일치를 검사합니다.
CI는 Windows/macOS/Linux의 Python 3.11·3.13에서 전체 검사와 JUnit 결과 게시를 수행합니다.
macOS/Linux의 별도 Hermes native job은 고정한 공개 Hermes 소스로 실제 플러그인 로더·도구 등록·훅·작업 경로 계약을 검사합니다. 모델·외부 메시징·인증정보를 사용하지 않습니다.
저장소의 `pytest.ini`가 패키지 경로와 테스트 검색 범위를 정합니다.
테스트 파일에서 스킬의 구현 디렉터리를 Python 검색 경로로 추가하지 않습니다.
기존 `python plugins/chef/codex/sync_shared.py`는 체크아웃에서만 쓰는 개발 명령 별칭으로 유지합니다.

Hermes 회귀 검사는 가상의 프로젝트·분리된 홈·가짜 native host/CLI와 bridge로 가드, 인자 전달, 소유권과 오류 처리를 확인합니다.
Claude 구독 backend도 가짜 CLI로 로그인 검사·hook handshake·도구 제한을 확인합니다. 이 결과를 실제 Hermes gateway나
구독 모델 호출의 검증으로 대신하지 않습니다. 실제 검증은 macOS·Linux 각각의
서비스 실행 계정에서 GUI 없이 슬롯 선택, 미션 시작, 역할 파견, 질문 응답과 재시작 후 재개를 확인합니다.
구독 인증·native API·지원 OS를 실제로 확인하지 못한 항목은 완료로 보고하지 않습니다.

## 운영 절차의 정본

- [미션 진행·승인·역할 분리](../plugins/chef/shared/skills/mission.md)
- [검증·재사용·독립 리뷰](../plugins/chef/skills/mission/references/verification-workflow.md)
- [질문 정본·답 이력](../plugins/chef/skills/mission/references/question-records.md)
- [사람용 문서 작성](../plugins/chef/skills/mission/references/human-writing.md)
- [파견·사용량·리뷰 운영](../plugins/chef/skills/mission/references/usage-policy.md)
- [슬롯 준비·실행](../plugins/chef/shared/skills/slots.md)
- [피드백 정리](../plugins/chef/shared/skills/distill.md)
- [실행 통계·작업 대기열·모델 선택 추천](autonomous-lab.md)

미션·슬롯 상태와 잠금을 공유하므로 같은 작업 공간의 세션을 동시에 이어받지 않습니다.
대시보드는 상태와 검토된 문서를 읽는 화면이며 승인·실행·자동 재개는 기존 미션 경로를 따릅니다.

## 변경 반영

버전·공개 전 점검·commit/push·런타임 반영 확인은 [완료 절차](../AGENTS.md#완료-절차)를 따릅니다.
설치본은 캐시 복사본일 수 있으므로 소스 수정만으로 반영을 판단하지 않습니다.
업데이트와 재시작 방법은 [설치 안내](install.md#업데이트)에 있습니다.
문서만 바꾸고 배포 파일이 같으면 플러그인 버전을 올리지 않습니다.
