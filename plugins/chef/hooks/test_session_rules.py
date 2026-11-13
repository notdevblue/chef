import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))

import session_rules

SCRIPT = HOOKS_DIR / "session_rules.py"
CLEARED_ENV = (
    "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR", "HAN_ADAPTERS_DIR",
    "GROK_HOOK_EVENT", "GROK_SESSION_ID",
)
SUBAGENT_START = {"hook_event_name": "SubagentStart", "agent_id": "a1"}


def run_hook(env=None, payload=None, raw=None):
    base = {k: v for k, v in os.environ.items() if k not in CLEARED_ENV}
    base.update(env or {})
    if raw is None:
        stdin = json.dumps(payload or {}).encode("utf-8")
    else:
        stdin = raw
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT)], input=stdin, capture_output=True, env=base, timeout=30
    )
    assert result.returncode == 0
    text = result.stdout.decode("ascii").strip()
    if text:
        return json.loads(text)
    return {}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1]", b'{}'])
def test_invalid_or_empty_payload_outputs_nothing(raw):
    assert run_hook(raw=raw) == {}


@pytest.mark.parametrize("event", ["SubagentStart", "Other", 5, ["SessionStart"]])
def test_other_events_do_not_export_paths(tmp_path, event):
    assert run_hook(registry_env(tmp_path), {"hook_event_name": event, "transcript_path": "C:/session.jsonl"}) == {}
    assert env_lines(tmp_path) == []


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
@pytest.mark.parametrize("value", ["on", "shared"])
def test_legacy_rule_options_never_read_or_inject_files(tmp_path, monkeypatch, runtime, value):
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "personal-rules.md").write_bytes(b"\xff\xfe")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("GROK_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_INJECT_RULES", value)
    monkeypatch.setenv(f"HAN_{runtime.upper()}_INJECT_RULES", value)
    monkeypatch.setenv("HAN_RULES_DISABLE", "0")
    assert session_rules.decide({"hook_event_name": "SessionStart"}, runtime) == {}
    assert session_rules.decide(SUBAGENT_START, runtime) == {}


def test_plugin_no_longer_distributes_rules_or_injection_option():
    manifest = json.loads((HOOKS_DIR.parent / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert set(manifest["userConfig"]) == {"adapters_dir"}
    for folder in (HOOKS_DIR.parent / "rules", HOOKS_DIR.parent / "codex/rules"):
        assert not list(folder.glob("*.md"))


def test_hooks_do_not_register_subagent_start():
    for path in (HOOKS_DIR / "hooks.json", HOOKS_DIR.parent / "codex/hooks.json"):
        assert "SubagentStart" not in json.loads(path.read_text(encoding="utf-8"))["hooks"]


def registry_env(tmp_path, value=r"D:\Work\adapters"):
    return {"CLAUDE_ENV_FILE": str(tmp_path / "env.sh"), "CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR": value}


def env_lines(tmp_path):
    path = tmp_path / "env.sh"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_session_start_exports_registry_dir_to_env_file(tmp_path):
    output = run_hook(registry_env(tmp_path))
    assert env_lines(tmp_path) == ['export HAN_ADAPTERS_DIR="D:/Work/adapters"']
    assert "systemMessage" not in output


def test_export_appends_after_existing_lines_and_is_not_repeated(tmp_path):
    (tmp_path / "env.sh").write_text("export OTHER=1\n", encoding="utf-8")
    run_hook(registry_env(tmp_path))
    run_hook(registry_env(tmp_path))
    assert env_lines(tmp_path) == ["export OTHER=1", 'export HAN_ADAPTERS_DIR="D:/Work/adapters"']


def test_export_escapes_quote_dollar_and_backtick(tmp_path):
    run_hook(registry_env(tmp_path, r'C:\a "b"\$x\`y'))
    assert env_lines(tmp_path) == [r'export HAN_ADAPTERS_DIR="C:/a \"b\"/\$x/\`y"']


@pytest.mark.parametrize(
    "case",
    ["no_env_file", "empty_option", "han_env_already_set", "no_option"],
)
def test_export_is_skipped(tmp_path, case):
    env = registry_env(tmp_path)
    if case == "no_env_file":
        del env["CLAUDE_ENV_FILE"]
    elif case == "empty_option":
        env["CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR"] = "  "
    elif case == "han_env_already_set":
        env["HAN_ADAPTERS_DIR"] = "E:/already"
    else:
        del env["CLAUDE_PLUGIN_OPTION_ADAPTERS_DIR"]
    run_hook(env)
    assert env_lines(tmp_path) == []


def test_subagent_start_does_not_touch_env_file(tmp_path):
    run_hook(registry_env(tmp_path), payload=SUBAGENT_START)
    assert env_lines(tmp_path) == []


def test_export_still_happens_when_rules_are_disabled(tmp_path):
    output = run_hook({**registry_env(tmp_path), "HAN_RULES_DISABLE": "1"})
    assert output == {}
    assert len(env_lines(tmp_path)) == 1


def test_export_write_failure_warns(tmp_path):
    folder = tmp_path / "env.sh"
    folder.mkdir()
    output = run_hook(registry_env(tmp_path))
    assert "HAN_ADAPTERS_DIR" in output["systemMessage"]
    assert "CLAUDE_ENV_FILE" in output["systemMessage"]


def transcript_payload(path=r"C:\Users\example\.claude\projects\p\s1.jsonl"):
    return {"hook_event_name": "SessionStart", "transcript_path": path}


def transcript_env(tmp_path):
    return {"CLAUDE_ENV_FILE": str(tmp_path / "env.sh")}


def test_session_start_exports_transcript_path_to_env_file(tmp_path):
    output = run_hook(transcript_env(tmp_path), payload=transcript_payload())
    assert env_lines(tmp_path) == ['export HAN_TRANSCRIPT="C:/Users/example/.claude/projects/p/s1.jsonl"']
    assert "systemMessage" not in output


def test_transcript_export_is_not_repeated_for_same_path(tmp_path):
    run_hook(transcript_env(tmp_path), payload=transcript_payload())
    run_hook(transcript_env(tmp_path), payload=transcript_payload())
    assert len(env_lines(tmp_path)) == 1


def test_transcript_export_appends_for_different_path_even_if_env_already_set(tmp_path):
    env = {**transcript_env(tmp_path), "HAN_TRANSCRIPT": "C:/old.jsonl"}
    run_hook(env, payload=transcript_payload("C:/old.jsonl"))
    run_hook(env, payload=transcript_payload("C:/new.jsonl"))
    assert env_lines(tmp_path) == ['export HAN_TRANSCRIPT="C:/old.jsonl"', 'export HAN_TRANSCRIPT="C:/new.jsonl"']


@pytest.mark.parametrize("payload", [{}, {"transcript_path": ""}, {"transcript_path": "  "}, {"transcript_path": 5}])
def test_transcript_export_is_skipped_without_usable_path(tmp_path, payload):
    run_hook(transcript_env(tmp_path), payload=payload)
    assert env_lines(tmp_path) == []


def test_transcript_export_is_skipped_without_env_file(tmp_path):
    output = run_hook(payload=transcript_payload())
    assert "systemMessage" not in output
    assert env_lines(tmp_path) == []


def test_transcript_export_still_happens_when_rules_are_disabled(tmp_path):
    output = run_hook({**transcript_env(tmp_path), "HAN_RULES_DISABLE": "1"}, payload=transcript_payload())
    assert output == {}
    assert len(env_lines(tmp_path)) == 1


def test_transcript_export_is_skipped_for_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "env.sh"))
    session_rules.decide(transcript_payload(), "codex")
    assert env_lines(tmp_path) == []


def test_subagent_start_does_not_export_transcript_path(tmp_path):
    run_hook(transcript_env(tmp_path), payload={**SUBAGENT_START, "transcript_path": "C:/a.jsonl"})
    assert env_lines(tmp_path) == []


def test_transcript_export_write_failure_warns(tmp_path):
    (tmp_path / "env.sh").mkdir()
    output = run_hook(transcript_env(tmp_path), payload=transcript_payload())
    assert "HAN_TRANSCRIPT" in output["systemMessage"]
    assert "CLAUDE_ENV_FILE" in output["systemMessage"]


def test_hooks_json_registers_session_start_for_every_source():
    hooks = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    group = hooks["SessionStart"][0]
    assert set(group["matcher"].split("|")) >= {"startup", "resume", "clear", "compact"}
    handler = group["hooks"][0]
    assert handler["command"] == "python"
    assert handler["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/session_rules.py"]
    assert "PreToolUse" in hooks


def test_all_runtime_and_marketplace_versions_match():
    manifest = json.loads((HOOKS_DIR.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((HOOKS_DIR.parents[2] / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["plugins"][0]["version"] == manifest["version"]
    for folder in (".codex-plugin", ".grok-plugin"):
        runtime = json.loads((HOOKS_DIR.parent / folder / "plugin.json").read_text(encoding="utf-8"))
        assert runtime["version"] == manifest["version"]


def test_plugin_json_declares_adapters_dir_user_config():
    manifest = json.loads((HOOKS_DIR.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    option = manifest["userConfig"]["adapters_dir"]
    assert {"type", "title", "description"} <= set(option)
    assert option["type"] == "string"
    assert not option.get("sensitive")
