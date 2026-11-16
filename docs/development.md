# 개발·검증

개발·설치·업데이트의 기준 저장소는 `notdevblue/chef`입니다.
기여할 때는 [저장소 지침과 공개 경계](../AGENTS.md)를 따릅니다.
회사·고객 자료, 개인 어댑터, 원본 대화·로그와 비밀정보는 배포본에 넣지 않습니다.

## 코드 구조

| 구성 | 위치 |
| --- | --- |
| 세 런타임 공통 스킬 절차 | `plugins/chef/shared/skills/` |
| Claude 진입점·역할 | `plugins/chef/skills/`, `plugins/chef/agents/` |
| Codex 진입점·역할 | `plugins/chef/codex/` |
| Grok 진입점·역할 | `plugins/chef/grok/` |
| 공유 가드·훅 | `plugins/chef/hooks/` |
| 미션·검증·실행 관리 구현 | `plugins/chef/skills/mission/scripts/` |
| 슬롯·읽기 전용 대시보드 | `plugins/chef/skills/slots/scripts/` |

공통 계약은 공유 절차에서 관리하고 런타임 진입점은 도구·역할·경로 차이만 둡니다.
역할의 실제 모델·effort는 각 런타임 정의와 참조 문서를 확인합니다.

## 테스트

저장소 루트에서 실행합니다.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest plugins/chef -q -p no:cacheprovider
python plugins/chef/codex/sync_shared.py
```

변경 범위에 맞춰 각 디렉터리의 pytest를 실행할 수도 있습니다.
가드는 `plugins/chef/hooks`, 미션·검증은 `plugins/chef/skills/mission/scripts`,
슬롯·대시보드는 `plugins/chef/skills/slots/scripts`, doctor는 `plugins/chef/skills/doctor/scripts`입니다.
Codex 전용 검사는 `plugins/chef/codex/test_codex.py`, Grok 전용 검사는 `plugins/chef/grok`에 있습니다.
전용 검사만 실행한 결과를 세 런타임 전체 검증으로 보고하지 않습니다.

어댑터의 가드 규칙을 고쳤으면 해당 어댑터도 확인합니다.

```text
python plugins/chef/skills/adapter-init/scripts/check_guard.py <어댑터 이름>
```

`sync_shared.py`는 공통 스킬·런타임 링크·참조 포인터를 검사합니다.
`--write`는 Codex 참조 포인터를 갱신합니다. 공통 절차의 사본을 런타임 폴더에 추가하지 않습니다.
CI는 Windows/Linux의 Python 3.11·3.13에서 전체 검사와 JUnit 결과 게시를 수행합니다.

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
