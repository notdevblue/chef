---
name: doctor
description: chef 설치와 어댑터 상태를 읽기 전용으로 점검한다(python 스텁, HAN_ADAPTERS_DIR·레지스트리, 어댑터·local.json 문제, guard-cases, ACTIVE 끊김 의심, Unity 경로, 저장소와 설치본 버전). "chef doctor", "chef 점검", "가드 안 도는 것 같아", "플러그인 버전 맞는지 봐줘", "/chef:doctor" 요청에 사용한다. 아무것도 고치거나 닫지 않는다.
---

# chef doctor

Bash로 실행한다.

```
python "${CLAUDE_PLUGIN_ROOT}/skills/doctor/scripts/doctor.py"
```

- 줄은 `OK|WARN|FAIL <영역>: <내용>`이다. FAIL이 있으면 종료 코드 1. `--json`이면 JSON.
- 결과를 사용자에게 요약할 때 FAIL과 WARN만 영역별로 묶어 원인과 다음 조치를 함께 말한다.
- `active` WARN(끊김 의심)은 **닫지 않는다.** 출력된 `close-stale` 미리보기 명령과 함께 보여 주고,
  닫기(`--force-close-stale`)는 사용자가 그 미션을 지정해 승인했을 때만 `/chef:mission` 절차로 한다.
- `version` WARN은 설치본이 저장소보다 오래됐다는 뜻이다. 업데이트는 저장소 AGENTS.md의 배포 절차를 따른다.
- `guard-cases` FAIL은 `python "${CLAUDE_PLUGIN_ROOT}/skills/adapter-init/scripts/check_guard.py" <어댑터>`로 어긋난 표본을 본다.
- `codex-agents` WARN은 `$CODEX_HOME/agents`(없으면 `~/.codex/agents/`)에 등록한 chef 역할 사본이 플러그인 `codex/agents/`와 내용이 다르거나(낡은 등록), 일부 역할이 빠졌거나, 플러그인에 없는 chef 역할 파일이 있다는 뜻이다. 사본을 고치지 말고 플러그인의 chef 역할을 모두 다시 복사한다. 등록이 없으면 OK(선택 사항)다.
- `role-policy` FAIL·WARN은 `adapter.json`·`local.json`의 `rolePolicy`(및 `runtimes.codex|grok.rolePolicy`)를 고친다. `model`은 `definition`/`inherit`, `subagents`는 true/false, 다른 키는 무시된다.
