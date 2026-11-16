# 실행 통계·작업 대기열·모델 선택 추천

세 런타임 모두 이 참조와 같은 폴더의 위쪽 `scripts/`에 있는 CLI를 사용한다.
`run_metrics.py --help`, `task_queue.py --help`, `model_recommendations.py --help`에서
정확한 인자를 확인한다. 이 기능은 선택 사항이며 미션 승인·검증·독립 리뷰는 기존 절차를 따른다.

- 실행 통계 (Run Metrics): 명시한 로컬 대화 기록과 검증·리뷰 기록의 메타데이터만 읽어 JSON/CSV로 내보낸다.
- 작업 대기열·배정 (Task Queue & Dispatch): 한 권한 디렉터리의 `queue.json`에서 우선순위·의존성·예산과 슬롯 예약을 관리한다.
  `dispatch`는 제안이고, `dispatch --execute`만 기존 슬롯 실행을 부른다. 미확인 실행은 자원을 유지한다.
- 모델 선택 추천 (Model Recommendations): 명시적인 동등 조건과 기준선을 비교해 추천 보고서를 만든다. 모델 설정은 쓰지 않는다.

기존 `observatory.py`·`supervisor.py`·`routing.py`와 저장 경로·환경변수는 호환을 유지한다.
Lab은 추가 개발·실험 범위에서 제외하며 기존 구현과 기록은 보존한다.

누락·미지원 관측은 `unknown`(JSON `null`)이다. 토큰은 크레딧이나 실제 과금이 아니다.
수집 결과와 대기열은 비공개 로컬 경로에 보존하고 원본 대화·회사 자료를 공개 저장소로 옮기지 않는다.

전체 설정과 복원 절차, 런타임 차이, 버전 마이그레이션, 실제 비교 실험과 한계는
[프로젝트 운영 문서](https://github.com/notdevblue/chef/blob/main/docs/autonomous-lab.md)에서 관리한다.
