---
name: doctor
description: chef 설치와 어댑터 상태를 읽기 전용으로 점검한다(python 스텁, HAN_ADAPTERS_DIR·레지스트리, 어댑터·local.json 문제, guard-cases, ACTIVE 끊김 의심, Unity 경로, 저장소와 설치본 버전). "chef doctor", "chef 점검", "가드 안 도는 것 같아", "플러그인 버전 맞는지 봐줘", "/chef:doctor" 요청에 사용한다. 아무것도 고치거나 닫지 않는다.
---

# Claude doctor

실행 전에 [공통 절차](../../shared/skills/doctor.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 두 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- Bash로 실행한다. `<doctor-script>`: `<plugin-root>/skills/doctor/scripts/doctor.py`, `<doctor-runtime>`: 빈 값.
- `<check-guard-script>`: `<plugin-root>/skills/adapter-init/scripts/check_guard.py`, `<check-guard-runtime>`: 빈 값.
- 사용자 승인 후 미션 처리는 `/chef:mission`으로 한다.
