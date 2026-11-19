---
name: slots
description: Chef slots for Hermes on macOS and Linux
---

# Hermes slots

[공통 절차](../../../shared/skills/slots.md)를 먼저 읽고 따른다.

`chef_slot(project="/absolute/project", name="slot1")`은 새 터미널 없이 현재 작업에 슬롯을 연결한다. 준비는 기존 slots.py에 `--runtime hermes`를 지정한다. `run`은 사용하지 않는다. 같은 소유 세션의 재개는 `resume=true`를 지정한다. dry_run은 연결·준비 단계를 실행하지 않는다.
