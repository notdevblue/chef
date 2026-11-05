---
name: chef-watch
description: "저장소 안내. Grok 스킬로 등록하지 않는다. chef 상태 조회는 chef-status, 어댑터 진단은 chef-doctor를 쓴다."
---

# chef-watch

이 폴더는 Grok에 등록하는 스킬이 아니다. 예전에 상태를 보던 이름이다.

- 상태 조회는 `plugins/chef/grok/skills/chef-status/SKILL.md`
- 어댑터를 못 찾으면 `plugins/chef/grok/skills/chef-doctor/SKILL.md`
- 메인 또는 슬롯의 실행 봇을 깨우는 인계는 `plugins/chef/grok/skills/chef-mission-brief/SKILL.md`
- 구현 절차는 `plugins/chef/grok/skills/chef-run/SKILL.md`
- 미션 상태 쓰기는 `plugins/chef/grok/skills/chef-mission/SKILL.md`
- 슬롯 조작은 `plugins/chef/grok/skills/chef-slots/SKILL.md`
- 어댑터 초안은 `plugins/chef/grok/skills/chef-adapter-init/SKILL.md`
- 피드백 정리는 `plugins/chef/grok/skills/chef-distill/SKILL.md`

Grok에 등록하는 스킬은 `chef-status`, `chef-doctor`, `chef-mission-brief`, `chef-run`, `chef-mission`, `chef-slots`, `chef-adapter-init`, `chef-distill`이다. 이 폴더는 등록하지 않는다.
