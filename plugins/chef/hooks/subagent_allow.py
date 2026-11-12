import fnmatch
import json
import sys

import mission_guard
from hook_runtime import normalize_payload

SPAWN_TOOLS = {
    "claude": ("Agent", "Task"),
    "grok": ("spawn_subagent", "Task", "Agent"),
}
CODEX_SPAWN_SUFFIX = "spawn_agent"
TYPE_KEYS = {"claude": "subagent_type", "grok": "subagent_type", "codex": "agent_type"}
DEFAULT_TYPES = {"claude": "general-purpose", "grok": "general-purpose", "codex": "default"}
NON_AGENT_ROLE_KEYS = ("reviewLoopSkill",)
ROLE_DEFAULTS = (
    ("research", "chef:researcher"),
    ("implement", "chef:dev-medium"),
    ("verify", "chef:verifier"),
    ("review", "chef:code-reviewer"),
    ("reviewSmall", "chef:reviewer-small"),
)
LEGACY_NAMES = {
    "chef_researcher": "chef:researcher",
    "chef_developer_small": "chef:dev-small",
    "chef_developer_medium": "chef:dev-medium",
    "chef_verifier": "chef:verifier",
    "chef_code_reviewer": "chef:code-reviewer",
    "chef_reviewer_small": "chef:reviewer-small",
}
GROK_REVIEW_NAMES = {"code-reviewer": "chef:code-reviewer", "chef:reviewer": "chef:code-reviewer"}
OFF_PREFIX = "서브에이전트 허용 목록 꺼짐"


def is_spawn_tool(runtime, tool_name):
    if not isinstance(tool_name, str):
        return False
    if runtime == "codex":
        # Codex 는 도구 이름 앞에 namespace 가 붙음(collaborationspawn_agent)
        return tool_name.endswith(CODEX_SPAWN_SUFFIX)
    return tool_name in SPAWN_TOOLS.get(runtime, SPAWN_TOOLS["claude"])


def requested_type(payload):
    """(이름, 읽지 못한 이유). 이유가 있으면 이름을 판정할 수 없음."""
    runtime = payload.get("han_runtime", "claude")
    if runtime == "grok" and payload.get("toolInputTruncated"):
        return "", "도구 입력이 잘려 서브에이전트 이름을 판정할 수 없음"
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            return "", "도구 입력을 JSON 으로 읽을 수 없어 서브에이전트 이름을 판정할 수 없음"
    if not isinstance(tool_input, dict):
        return "", "도구 입력이 객체가 아니어서 서브에이전트 이름을 판정할 수 없음"
    keys = (TYPE_KEYS[runtime],) + (("subagentType",) if runtime == "grok" else ())
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), ""
    return DEFAULT_TYPES[runtime], ""


def allow_patterns(adapter):
    """(패턴 목록 또는 None, 형식 문제). 키가 없으면 (None, "") — 제한 없음."""
    agents = adapter.get("agents")
    if agents is None:
        return None, ""
    if not isinstance(agents, dict):
        return None, "agents 형식 오류: 객체여야 함. 허용 목록을 적용하지 않음"
    allow = agents.get("allow")
    if allow is None:
        return None, ""
    if not isinstance(allow, list) or not all(isinstance(item, str) and item.strip() for item in allow):
        return None, "agents.allow 형식 오류: 빈 값이 없는 문자열 배열이어야 함. 허용 목록을 적용하지 않음"
    return [item.strip() for item in allow], ""


def allows(patterns, name):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def role_names(adapter, runtime="claude"):
    """roles 가 가리키는 실제 파견 이름. 없는 역할은 chef 기본 이름, 옛 이름은 현재 이름으로 읽음."""
    roles = adapter.get("roles")
    if not isinstance(roles, dict):
        roles = {}

    def named(key, default):
        value = roles.get(key)
        if isinstance(value, str) and value.strip():
            return LEGACY_NAMES.get(value.strip(), value.strip())
        return default

    names = {}
    for key, default in ROLE_DEFAULTS:
        names[key] = named(key, default)
    if runtime == "claude":
        implement_small_default = names["implement"]
    else:
        implement_small_default = "chef:dev-small"
    names["implementSmall"] = named("implementSmall", implement_small_default)
    names["audit"] = named("audit", names["review"])
    if runtime == "grok":
        names = {key: GROK_REVIEW_NAMES.get(value, value) if key in ("review", "audit") else value for key, value in names.items()}
    custom = [
        value.strip() for key, value in roles.items()
        if key not in NON_AGENT_ROLE_KEYS and key not in names and isinstance(value, str) and value.strip()
    ]
    return list(dict.fromkeys([*names.values(), *custom]))


def deny_reason(name, patterns, adapter, runtime="claude"):
    listed = ", ".join(role_names(adapter, runtime))
    return (
        f"서브에이전트 '{name}' 은(는) 어댑터 agents.allow 에 맞지 않아 파견할 수 없음. "
        f"허용 패턴: {', '.join(patterns)}. 어댑터 roles 의 이름: {listed}. 허용되는 이름으로 다시 호출할 것. "
        "맞는 역할이 없으면 다른 에이전트로 대신하지 말고 사용자에게 알릴 것"
    )


def decide(payload):
    payload = normalize_payload(payload)
    outcome = mission_guard.Outcome()
    runtime = payload["han_runtime"]
    if not is_spawn_tool(runtime, payload.get("tool_name")):
        return outcome
    located = mission_guard.locate_adapter(payload)
    if not located:
        return outcome
    problems = []
    adapter = mission_guard.load_adapter(located.adapter_path, problems, runtime=runtime)
    patterns, problem = allow_patterns(adapter)
    if problem:
        outcome.messages.append(f"{OFF_PREFIX}: {problem}")
        return outcome
    if patterns is None:
        return outcome
    outcome.messages.extend(problems)
    name, unreadable = requested_type(payload)
    if unreadable:
        outcome.messages.append(f"{OFF_PREFIX}: {unreadable}")
    elif not allows(patterns, name):
        outcome.deny = deny_reason(name, patterns, adapter, runtime)
    return outcome


def render_output(outcome):
    output = {}
    if outcome.messages:
        output["systemMessage"] = " / ".join(outcome.messages)
    if outcome.deny:
        output["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": outcome.deny,
        }
    return output


def main(runtime=""):
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        payload = payload if isinstance(payload, dict) else {}
        outcome = decide(normalize_payload(payload, runtime))
    except mission_guard.GuardOff as e:
        outcome = mission_guard.Outcome(messages=[f"{OFF_PREFIX}: {e}"])
    except Exception as e:  # 허용 목록 오류로 도구를 막지 않음
        outcome = mission_guard.Outcome(messages=[f"{OFF_PREFIX}: 내부 오류 ({e})"])
    output = render_output(outcome)
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=True))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
