# Comet 미션 어댑터 예시

이 예시는 처음부터 만든 작은 Python 프로젝트다. macOS·Linux·Windows에서 셸 배치 파일이나 게임 엔진 없이 슬롯 준비를 확인한다. `project/`를 별도 작업 폴더에 복사하고, `local.example.json`을 개인 `local.json`으로 복사한 뒤 프로젝트와 슬롯 상위 폴더의 절대 경로를 지정한다. 개인 경로·인증·업무 규칙은 공개 Git에 추가하지 않는다.

## 준비와 검증

공통 준비는 필요한 실행 파일·입력을 먼저 검사하고, 소스와 도구를 독립 슬롯에 복사한 뒤 설정·검증을 실행한다. 명령은 `argv` 배열이며 `{python}`은 Chef를 실행한 Python, `{plugin}`은 설치된 플러그인 경로다. 셸 변환이나 `cd` 상태 유지에 의존하지 않는다.

예시의 `tools/project.py verify --root <slot>`은 소스를 컴파일하고 작은 동작 검사를 실행한다. 실제 프로젝트에서는 이 명령을 해당 프로젝트의 빌드·테스트 진입점으로 교체한다. 필요한 외부 도구·스크립트는 `preflight`에서 먼저 검사한다. 다른 OS의 명령을 건너뛰어 성공으로 표시하지 않는다.

복사 도구는 기존 파일의 내용이 다르거나 링크·특수 노드가 있으면 멈춘다. 파일을 삭제하거나 덮어쓰지 않으므로, 바뀐 메인 소스를 기존 슬롯에 적용하려면 프로젝트의 VCS 동기화 또는 별도의 검토한 갱신 절차를 설정해야 한다. 예제의 `vcs: none`은 P4나 Git 작업 공간을 만드는 기능이 아니다.

## 런타임과 OS 설정

`adapter.json`의 `runtimes.claude.slots.beforeRun`만 Claude 자동 메모리 디렉터리를 연결한다. Codex·Grok·Hermes에는 이 단계가 상속되지 않는다. Windows는 native junction, macOS·Linux는 directory symlink를 만든다. 올바른 기존 링크는 유지하고, 다른 대상·일반 파일·일반 폴더는 교체하지 않는다.

기계별 값은 `local.json`에 둔다. OS별 차이는 `platforms.windows`, `platforms.macos`, `platforms.linux`로, 런타임 차이는 `runtimes.<runtime>`로 지정한다. 각 파일은 공통 → OS → 런타임 → 해당 런타임의 OS 순서로 병합하며, `local.json` 전체가 마지막이다. 객체는 깊게 병합하고 배열은 교체한다.

선택적인 에디터는 `slots.editorArgv`에 실제 실행 파일과 인자를 지정한다. 설치된 도구 경로를 예시의 기본값으로 가정하지 않는다. Hermes는 현재 세션에 `chef_slot`으로 연결한다. 다른 런타임의 새 세션은 설정한 터미널 백엔드를 사용한다.

## 선택적인 캐시

엔진 Library 같은 재사용 캐시는 `cache-copy.example.json`의 단계 형태를 참고한다. 원본 캐시와 대상 프로젝트의 fingerprint가 OS·CPU·도구 버전·backend·입력에서 모두 같을 때만 복사한다. metadata가 없거나 다르면 복사를 건너뛰고 다음 import 단계를 반드시 실행한다. 캐시를 심볼릭 링크로 공유하지 않는다.

## 미션과 프로젝트 정책

미션의 계획·질문·승인·검증·리뷰는 공통 스킬 절차를 따른다. 실제 프로젝트의 규칙은 해당 프로젝트 문서 경로를 읽고 개인 어댑터에 연결한다. 예시의 가드 표본은 셸 문법·VCS 명령 판정의 회귀 자료이며 제출·배포 권한을 부여하지 않는다.
