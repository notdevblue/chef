import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent
PLUGIN = HOOKS.parent
sys.path.insert(0, str(HOOKS))

import subagent_allow  # noqa: E402

ADAPTER_DIRS = {"claude": ".claude", "codex": ".codex", "grok": ".grok"}
SPAWN = {"claude": "Agent", "codex": "collaborationspawn_agent", "grok": "spawn_subagent"}
TYPE_KEY = {"claude": "subagent_type", "codex": "agent_type", "grok": "subagent_type"}
ROLES = {"dev": "team-dev", "review": "chef:code-reviewer", "reviewLoopSkill": "team-loop-skill"}
ALLOW = {"allow": ["team-*", "chef:*"]}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("HAN_ADAPTERS_DIR", "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR", "GROK_HOOK_EVENT", "GROK_SESSION_ID", "GROK_WORKSPACE_ROOT"):
        monkeypatch.delenv(name, raising=False)


def write_adapter(project, runtime, extra=None, local=None):
    folder = project / ADAPTER_DIRS[runtime] / "orchestrator"
    folder.mkdir(parents=True, exist_ok=True)
    adapter = {"schema": 1, "project": "t", "paths": {"stateDir": "state"}, "roles": ROLES}
    adapter.update(extra or {})
    (folder / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    if local is not None:
        (folder / "local.json").write_text(local if isinstance(local, str) else json.dumps(local), encoding="utf-8")
    return folder / "adapter.json"


def payload_of(project, runtime, name=None, tool_name=None, tool_input=None):
    if tool_input is None:
        tool_input = {"description": "x", "prompt": "y"}
        if name is not None:
            tool_input[TYPE_KEY[runtime]] = name
    tool_name = tool_name or SPAWN[runtime]
    if runtime == "grok":
        return {"hookEventName": "pre_tool_use", "toolName": tool_name, "toolInput": tool_input, "cwd": str(project), "sessionId": "s"}
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input, "cwd": str(project), "session_id": "s"}


def run_hook(project, runtime, payload):
    script = PLUGIN / ("codex/subagent_allow.py" if runtime == "codex" else "hooks/subagent_allow.py")
    env = dict(os.environ, PYTHONUTF8="1", CLAUDE_PROJECT_DIR=str(project))
    for name in ("HAN_ADAPTERS_DIR", "GROK_HOOK_EVENT", "GROK_SESSION_ID"):
        env.pop(name, None)
    done = subprocess.run([sys.executable, "-B", str(script)], input=json.dumps(payload).encode(), capture_output=True, timeout=20, env=env)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return json.loads(done.stdout or b"{}")


def decision(project, runtime, name=None, **kwargs):
    return run_hook(project, runtime, payload_of(project, runtime, name, **kwargs))


def denial(output):
    specific = output.get("hookSpecificOutput", {})
    if specific.get("permissionDecision") == "deny":
        return specific["permissionDecisionReason"]
    return ""


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_matching_name_passes_and_other_name_is_denied(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": ALLOW})
    assert decision(tmp_path, runtime, "team-dev") == {}
    assert decision(tmp_path, runtime, "chef:verifier") == {}
    output = decision(tmp_path, runtime, "outsider")
    reason = denial(output)
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "outsider" in reason and "team-*" in reason and "chef:*" in reason
    assert "team-dev" in reason and "chef:code-reviewer" in reason
    assert "team-loop-skill" not in reason


@pytest.mark.parametrize("runtime,default", [("claude", "general-purpose"), ("codex", "default"), ("grok", "general-purpose")])
def test_omitted_name_is_judged_by_runtime_default(tmp_path, runtime, default):
    write_adapter(tmp_path, runtime, {"agents": {"allow": ["team-*"]}})
    assert default in denial(decision(tmp_path, runtime, None))
    write_adapter(tmp_path, runtime, {"agents": {"allow": ["team-*", default]}})
    assert decision(tmp_path, runtime, None) == {}


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_blank_or_non_string_name_falls_back_to_default(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": {"allow": ["team-*"]}})
    for value in ("", "  ", 3, None):
        output = decision(tmp_path, runtime, tool_input={TYPE_KEY[runtime]: value})
        assert denial(output)


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_without_agents_key_or_adapter_passes(tmp_path, runtime):
    assert decision(tmp_path, runtime, "anything") == {}
    write_adapter(tmp_path, runtime)
    assert decision(tmp_path, runtime, "anything") == {}
    write_adapter(tmp_path, runtime, {"agents": {}})
    assert decision(tmp_path, runtime, "anything") == {}


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_allow_applies_regardless_of_active_mission(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": ALLOW})
    assert not (tmp_path / "state" / "ACTIVE").exists()
    assert denial(decision(tmp_path, runtime, "outsider"))


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_empty_allow_denies_everything(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": {"allow": []}})
    assert denial(decision(tmp_path, runtime, "team-dev"))


@pytest.mark.parametrize("runtime", ["codex", "grok"])
def test_runtime_overlay_replaces_top_level_allow(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": {"allow": ["team-*"]}, "runtimes": {runtime: {"agents": {"allow": ["other-*"]}}}})
    assert denial(decision(tmp_path, runtime, "team-dev"))
    assert decision(tmp_path, runtime, "other-x") == {}


def test_claude_ignores_runtime_overlay_and_uses_top_level(tmp_path):
    write_adapter(tmp_path, "claude", {"agents": {"allow": ["team-*"]}, "runtimes": {"claude": {"agents": {"allow": ["other-*"]}}}})
    assert decision(tmp_path, "claude", "team-dev") == {}
    assert denial(decision(tmp_path, "claude", "other-x"))


def test_local_json_overrides_adapter(tmp_path):
    write_adapter(tmp_path, "claude", {"agents": {"allow": ["team-*"]}}, local={"agents": {"allow": ["local-*"]}})
    assert denial(decision(tmp_path, "claude", "team-dev"))
    assert decision(tmp_path, "claude", "local-a") == {}


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_other_tools_are_not_checked(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": {"allow": []}})
    assert decision(tmp_path, runtime, "x", tool_name="Bash") == {}
    assert decision(tmp_path, runtime, "x", tool_name="collaborationwait_agent") == {}


def test_claude_task_alias_is_checked(tmp_path):
    write_adapter(tmp_path, "claude", {"agents": ALLOW})
    assert denial(decision(tmp_path, "claude", "outsider", tool_name="Task"))


def test_grok_accepts_task_alias_and_camel_case_type(tmp_path):
    write_adapter(tmp_path, "grok", {"agents": ALLOW})
    assert denial(decision(tmp_path, "grok", "outsider", tool_name="Task"))
    assert decision(tmp_path, "grok", tool_input={"subagentType": "team-dev"}) == {}
    assert denial(decision(tmp_path, "grok", tool_input={"subagentType": "outsider"}))


def test_codex_matches_namespaced_spawn_tool_names(tmp_path):
    write_adapter(tmp_path, "codex", {"agents": ALLOW})
    for tool in ("collaborationspawn_agent", "spawn_agent"):
        assert denial(decision(tmp_path, "codex", "outsider", tool_name=tool))


def test_pattern_match_is_case_sensitive_glob():
    assert subagent_allow.allows(["team-*"], "team-a")
    assert subagent_allow.allows(["chef:?-dev"], "chef:x-dev")
    assert not subagent_allow.allows(["team-*"], "Team-a")
    assert not subagent_allow.allows(["team-*"], "xteam-a")


CHEF_DEFAULTS = ["chef:researcher", "chef:dev-medium", "chef:verifier", "chef:code-reviewer", "chef:reviewer-small"]


def test_role_names_skip_review_loop_skill_and_duplicates():
    adapter = {"roles": {"a": "team-a", "b": "team-a", "reviewLoopSkill": "skill", "c": 3, "d": ""}}
    assert subagent_allow.role_names(adapter) == [*CHEF_DEFAULTS, "team-a"]
    assert subagent_allow.role_names({}) == CHEF_DEFAULTS


def test_role_names_use_chef_defaults_for_missing_keys():
    adapter = {"roles": {"research": "team-research", "implement": "team-dev"}}
    assert subagent_allow.role_names(adapter, "claude") == [
        "team-research", "team-dev", "chef:verifier", "chef:code-reviewer", "chef:reviewer-small"]
    assert subagent_allow.role_names(adapter, "codex")[-1] == "chef:dev-small"
    assert subagent_allow.role_names({"roles": {"review": "team-review"}})[3] == "team-review"


def test_role_names_implement_small_and_audit_follow_defaults():
    adapter = {"roles": {"implement": "team-dev", "review": "team-review"}}
    claude = subagent_allow.role_names(adapter, "claude")
    assert "chef:dev-small" not in claude and claude.count("team-dev") == 1 and claude.count("team-review") == 1
    assert "chef:dev-small" in subagent_allow.role_names(adapter, "grok")
    explicit = {"roles": {"implementSmall": "team-small", "audit": "team-audit"}}
    assert {"team-small", "team-audit"} <= set(subagent_allow.role_names(explicit))


def test_role_names_map_legacy_names_and_grok_review_aliases():
    adapter = {"roles": {"research": "chef_researcher", "review": "code-reviewer", "verify": "chef_verifier"}}
    claude = subagent_allow.role_names(adapter, "claude")
    assert "chef_researcher" not in claude and "chef:researcher" in claude and "code-reviewer" in claude
    grok = subagent_allow.role_names(adapter, "grok")
    assert "code-reviewer" not in grok and "chef:code-reviewer" in grok


def test_deny_reason_lists_effective_names_and_says_not_to_substitute(tmp_path):
    write_adapter(tmp_path, "claude", {"roles": {"research": "team-research"}, "agents": {"allow": ["team-*"]}})
    reason = denial(decision(tmp_path, "claude", "outsider"))
    assert "chef:verifier" in reason and "team-research" in reason
    assert "다른 에이전트로 대신하지 말고 사용자에게 알릴 것" in reason


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_json_string_tool_input_is_parsed(tmp_path, runtime):
    write_adapter(tmp_path, runtime, {"agents": {"allow": ["team-*"]}})
    assert decision(tmp_path, runtime, tool_input=json.dumps({TYPE_KEY[runtime]: "team-dev"})) == {}
    assert denial(decision(tmp_path, runtime, tool_input=json.dumps({TYPE_KEY[runtime]: "outsider"})))
    assert denial(decision(tmp_path, runtime, tool_input=json.dumps({"prompt": "x"})))


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("tool_input", ["{broken", "[1]"])
def test_unreadable_tool_input_passes_with_notice(tmp_path, runtime, tool_input):
    write_adapter(tmp_path, runtime, {"agents": {"allow": ["team-*"]}})
    output = decision(tmp_path, runtime, tool_input=tool_input)
    assert not denial(output)
    assert output["systemMessage"].startswith("서브에이전트 허용 목록 꺼짐")


def test_grok_truncated_input_passes_with_notice(tmp_path):
    write_adapter(tmp_path, "grok", {"agents": {"allow": ["team-*"]}})
    payload = payload_of(tmp_path, "grok", "outsider")
    payload["toolInputTruncated"] = True
    output = run_hook(tmp_path, "grok", payload)
    assert not denial(output)
    assert "잘려" in output["systemMessage"]


@pytest.mark.parametrize("agents", ["team-*", ["team-*"], {"allow": "team-*"}, {"allow": ["team-*", 3]}, {"allow": [""]}])
def test_malformed_allow_passes_with_notice(tmp_path, agents):
    write_adapter(tmp_path, "claude", {"agents": agents})
    output = decision(tmp_path, "claude", "outsider")
    assert not denial(output)
    assert output["systemMessage"].startswith("서브에이전트 허용 목록 꺼짐")


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_unreadable_adapter_passes_with_notice(tmp_path, runtime):
    folder = write_adapter(tmp_path, runtime, {"agents": ALLOW})
    folder.write_text("{broken", encoding="utf-8")
    output = decision(tmp_path, runtime, "outsider")
    assert not denial(output)
    assert output["systemMessage"].startswith("서브에이전트 허용 목록 꺼짐")


def test_broken_runtimes_overlay_passes_with_notice(tmp_path):
    write_adapter(tmp_path, "codex", {"agents": ALLOW, "runtimes": {"codex": "bad"}})
    output = decision(tmp_path, "codex", "outsider")
    assert not denial(output)
    assert "꺼짐" in output["systemMessage"]


def test_broken_local_json_is_reported_when_allow_applies(tmp_path):
    write_adapter(tmp_path, "claude", {"agents": ALLOW}, local="{broken")
    output = decision(tmp_path, "claude", "outsider")
    assert denial(output)
    assert "local.json" in output["systemMessage"]


def test_invalid_stdin_passes_without_output(tmp_path):
    script = PLUGIN / "hooks/subagent_allow.py"
    done = subprocess.run([sys.executable, "-B", str(script)], input=b"not json", capture_output=True, timeout=20)
    assert done.returncode == 0
    assert json.loads(done.stdout)["systemMessage"].startswith("서브에이전트 허용 목록 꺼짐")


def test_output_is_ascii_json(tmp_path):
    write_adapter(tmp_path, "claude", {"agents": ALLOW})
    script = PLUGIN / "hooks/subagent_allow.py"
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path))
    done = subprocess.run([sys.executable, "-B", str(script)], input=json.dumps(payload_of(tmp_path, "claude", "outsider")).encode(),
                          capture_output=True, timeout=20, env=env)
    done.stdout.decode("ascii")


def registered(path, key):
    hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    return [entry for entry in hooks if "subagent_allow.py" in json.dumps(entry)], hooks, key


def test_claude_and_grok_share_hooks_json_registration():
    entries, hooks, _ = registered(PLUGIN / "hooks/hooks.json", "claude")
    assert len(entries) == 1
    matcher = entries[0]["matcher"].split("|")
    assert {"Agent", "Task", "spawn_subagent"} == set(matcher)
    assert entries[0]["hooks"][0]["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/subagent_allow.py"]
    assert "mission_guard.py" in json.dumps(hooks[0])
    assert not any(tool in hooks[0]["matcher"].split("|") for tool in matcher)


def test_codex_hooks_json_registers_spawn_agent_matcher():
    import re

    entries, hooks, _ = registered(PLUGIN / "codex/hooks.json", "codex")
    assert len(entries) == 1
    matcher = entries[0]["matcher"]
    assert re.fullmatch(matcher, "collaborationspawn_agent")
    assert not re.fullmatch(matcher, "Bash")
    assert "codex/subagent_allow.py" in entries[0]["hooks"][0]["command"]
    assert "mission_guard.py" in json.dumps(hooks[0])
    assert (PLUGIN / "codex/subagent_allow.py").is_file()
