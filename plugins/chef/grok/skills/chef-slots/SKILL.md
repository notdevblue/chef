---
name: chef-slots
description: "Grok CLI에서 슬롯 워크스페이스를 조회·생성·갱신·삭제할 때 쓴다. 슬롯 만들어줘, 슬롯 상태, 슬롯 갱신, 슬롯 지워줘, 슬롯에서 미션 돌려줘, chef-slots 요청에 사용한다. Grok 구현은 슬롯 폴더의 grok 와 chef-run 이다."
---

# Grok slots

실행 전에 [공통 절차](../../../shared/skills/slots.md) 전체를 읽는다. 공통 단계·승인 범위·독립성·증거 요구는 그 문서 한곳에서 관리한다. 이 파일은 런타임 실행 차이만 정한다.

`<plugin-root>`는 이 SKILL.md에서 세 단계 위인 플러그인 폴더다. 실제 절대 경로를 확인한다. 공통 문서의 경로 별칭은 아래 값으로 치환하고, 비어 있는 런타임 인자는 생략한다.

## 실행 계약

- `<slots-script>`: `<plugin-root>/skills/slots/scripts/slots.py`, `<slots-runtime>`: `--runtime grok`.
- `<dashboard-script>`: `<plugin-root>/skills/slots/scripts/dashboard.py`, `<dashboard-runtime>`: `--runtime grok`.
- `<repo>`는 `HAN_REPO` 또는 실제 체크아웃이며 `<plugin-root>`를 포함한다. 공통 [어댑터 계약](../../../skills/mission/references/adapter-schema.md)의 Grok 검색·병합을 쓴다.
- `slots.beforeRun`의 Grok 명령은 `runtimes.grok.slots.beforeRun`에 둔다.
- `create`·`refresh`는 `--dry-run`으로 기준·거부 사유를 보여 주고 사용자가 그 실행을 승인한 뒤 본명령을 돌린다. 로컬 변경 처리 플래그는 해당 처리를 승인받은 뒤에만 쓴다.

## Grok으로 슬롯 작업


새 미션은 비어 있는 ready 슬롯에만 붙인다. ACTIVE가 있는 슬롯에 다른 미션을 겹치지 않는다.
Grok CLI가 이미 등록된 슬롯 경로에서 실행 중이면 `chef-run`으로 구현한다. 새 Windows Terminal 세션이 필요할 때 사용자가 요청하면 아래 `run --runtime grok`을 쓴다.

`slots.py run`은 선택한 런타임 세션을 Windows Terminal에 띄운다. Claude는 `--runtime` 없이, Grok은 `--runtime grok`으로 호출한다. 두 경우 모두 슬롯이 `ready`이고 ACTIVE가 없어야 한다. 스크립트가 확인하는 소유권·상태 조건을 우회하지 않는다.
Codex 세션은 사용자가 그 런타임을 지정할 때만 `<repo>/plugins/chef/codex/skills/chef-slots/scripts/slots.py`의 `run`이다.

대시보드가 필요하면 `<repo>/plugins/chef/skills/slots/scripts/dashboard.py`를 읽기 전용으로 띄운다. 답과 승인은 대시보드에 쓰지 않는다.

## 답 넣기 (inject)

슬롯 Grok 세션에 사용자 답을 넣을 때는 `inject`만 쓴다. `grok -r -p`를 손으로 부르지 않는다.

```text
python "<repo>/plugins/chef/skills/slots/scripts/slots.py" --runtime grok --project "<메인 프로젝트>" inject <이름> --file <답.txt> [--session <id>] [--wait]
```

- `inject <이름> (--text <답> | --file <파일>) [--session <id>] [--wait [--timeout 초] [--poll 초]] [--dry-run]` — Grok 슬롯 세션에
  답을 넣는다(Grok 전용). 세션 id 를 안 주면 슬롯 `ACTIVE.session`이다. **세션이 쉬는 중일 때만** 넣는다: 마지막 턴이 `turn_completed`로
  끝났고, 끝나지 않은 도구 호출·실행 중인 서브에이전트(백그라운드 포함)·백그라운드 작업이 없고, 서브에이전트 깨우기(`will_wake`)로
  곧 시작될 턴도 없어야 한다(`~/.grok/sessions/<cwd>/<id>/updates.jsonl`·`events.jsonl`·`subagents/*/meta.json`으로 판정).
  아니면 이유를 보이고 종료 코드 3으로 거부한다(전역 `follow_up_behavior = "steer"`라 턴 중에 넣으면 진행 중 작업을 가로챔).
  `--wait`는 쉬는 중이 될 때까지 `--poll`초마다 보다가 `--timeout`초(기본 1800)가 지나면 거부한다.
  슬롯 리더가 살아 있으면 ACP 클라이언트로 그 리더에 붙어 넣어 열린 TUI 화면에 새 턴이 보인다. 리더가 없으면
  `grok --cwd <슬롯> -r <id> -p <답>`으로 넣되, 그 세션 TUI 가 열려 있으면(`~/.grok/active_sessions.json`·명령줄) 거부한다.
  새 턴이 기록되면 성공(0)이다. 넣는 프로세스는 백그라운드로 턴 끝까지 돌고 로그는 `<stateDir>/slots/inject/`에 남는다.
  세션 cwd 가 슬롯 폴더가 아니면 거부한다.
- `--runtime grok`의 `run`(새 세션·`--resume` 모두)은 TUI 를 리더 모드(`grok --leader --leader-socket ~/.grok/leader-<프로젝트>-<이름>.sock`)로
  띄우고 소켓 경로를 `<stateDir>/slots/<이름>-grok-leader.json`에 남긴다. 리더는 TUI 가 처음 뜰 때 스스로 띄우고 창을 닫아도 남는다.
  이 변경 전에 띄운 Grok 슬롯 창은 리더 모드가 아니라 `inject`가 TUI 화면에 못 보이므로, 다음 재개 때 `run --resume`으로 다시 열면 붙는다.
