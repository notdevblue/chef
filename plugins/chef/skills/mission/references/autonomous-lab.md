# 관측·감독·실험 도구

세 런타임 모두 이 참조와 같은 폴더의 위쪽 `scripts/`에 있는 CLI를 사용한다.
`observatory.py --help`, `supervisor.py --help`, `routing.py --help`, `lab.py --help`에서
정확한 인자를 확인한다. 이 기능은 선택 사항이며 미션 승인·검증·독립 리뷰는 기존 절차를 따른다.

- Observatory: 명시한 로컬 대화 기록과 검증·리뷰 기록의 메타데이터만 읽어 JSON/CSV로 내보낸다.
- Supervisor: 한 권한 디렉터리의 `queue.json`에서 우선순위·의존성·예산과 슬롯 예약을 관리한다.
  `dispatch`는 제안이고, `dispatch --execute`만 기존 슬롯 실행을 부른다. 미확인 실행은 자원을 유지한다.
- Routing: 명시적인 동등 조건과 기준선을 비교해 추천 보고서를 만든다. 모델 설정은 쓰지 않는다.
- Lab: 가상 과제용 기존 NoVcs 슬롯을 준비하고 독립 결과물을 고정·평가한다. 숨겨진 데이터는
  개발 슬롯 밖에 두며, 후보 코드 실행에는 Linux bubblewrap이 필요하다. 모델 호출은 별도 승인된 개발 경로다.

누락·미지원 관측은 `unknown`(JSON `null`)이다. 토큰은 크레딧이나 실제 과금이 아니다.
수집 결과와 대기열은 비공개 로컬 경로에 보존하고 원본 대화·회사 자료를 공개 저장소로 옮기지 않는다.

전체 설정과 복원 절차, 런타임 차이, 버전 마이그레이션, 실제 비교 실험과 한계는
[프로젝트 운영 문서](https://github.com/notdevblue/chef/blob/main/docs/autonomous-lab.md)에서 관리한다.
