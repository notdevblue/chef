---
name: mission
description: Chef mission for Hermes on macOS and Linux
---

# Hermes mission

[공통 절차](../../../shared/skills/mission.md)를 먼저 읽고 따른다.

Chef CLI는 native 문맥의 `Chef CLI Python executable` 절대 경로로 실행한다. 이 값은 실행 중인 Hermes의 Python 3.11+ 인터프리터다.

현재 Hermes 세션에서 수행한다. `chef_slot`으로 준비된 슬롯을 선택하고 모든 작업에 해당 작업 폴더를 적용한다. 미션 마커는 terminal 도구로 실제 실행해 가드를 통과한다. 역할 작업은 `chef_delegate(role="chef:dev-medium", prompt="...")`처럼 Codex CLI로 파견한다. `delegate_task`로 대체하거나 모델 API를 직접 호출하지 않는다. 사용자 답·승인을 추론하지 않고 기존 대화 채널에서 받는다.
