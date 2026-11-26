---
name: mission
description: Chef mission for Hermes on macOS and Linux
---

# Hermes mission

[공통 절차](../../../shared/skills/mission.md)를 먼저 읽고 따른다.

Chef CLI는 native 문맥의 `Chef CLI Python executable` 절대 경로로 실행한다. 이 값은 실행 중인 Hermes의 Python 3.11+ 인터프리터다.

현재 Hermes 세션에서 수행한다. `chef_slot`으로 준비된 슬롯을 선택한 뒤 `chef_worker_status`로 이번 작업에 필요한 역할이 지원되는지 확인한다. 지원되지 않는 역할이 필요한 경우 미션을 열기 전에 제한을 알린다. 선택한 슬롯을 모든 작업에 적용하고 미션 시작·재개·종료는 `chef_mission(action="open|resume|close", mission="<id>")`로 실행한다. 상태는 `chef_mission(action="status")`로 조회한다. terminal 미션 마커는 사용하지 않는다. 외부 슬롯은 설정된 진입 확인을 마친 뒤 `chef_mission`의 open 작업으로 예약을 획득하고 미션 초기화와 작업 명령을 실행한다.

역할 작업은 `chef_delegate`에서 어댑터의 실행 방식을 사용한다. 기본 `hermes` backend는 현재 Hermes 프로필의 모델과 인증을 사용하며 여섯 Chef 역할을 파견한다. `codex`, `claude`, 또는 명시적으로 구성한 JSONL bridge를 선택할 수도 있다. 사용자 답과 승인은 기존 대화 채널에서 받는다.
