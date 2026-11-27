"""Backend contracts use fictional accounts and executable results."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.workers import backends


def result(stdout="", code=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=code, stderr=stderr)


@pytest.mark.parametrize("text", [None, "intermediate prose"])
def test_grok_native_structured_end_is_authoritative(text):
    proposed = {"summary": "changed", "changes": []}
    rows = [] if text is None else [{"type": "text", "data": text}]
    rows.append({"type": "end", "stopReason": "end_turn", "structuredOutput": proposed})
    parsed = backends.parse_result("grok", result("\n".join(map(json.dumps, rows))), "dev-small")
    assert parsed["ok"] and json.loads(parsed["result"]) == proposed


def test_grok_structured_payload_cannot_override_failed_end():
    event = {"type": "end", "stopReason": "error", "structuredOutput": {"changes": []}}
    assert not backends.parse_result("grok", result(json.dumps(event)), "dev-small")["ok"]


@pytest.mark.parametrize("runtime", backends.RUNTIMES)
def test_native_candidate_never_authenticates_host(monkeypatch, tmp_path, runtime):
    called = []
    monkeypatch.setattr(backends, "auth", lambda name, *args: called.append(name))
    rows = backends.candidates({}, str(tmp_path), "implement", native=runtime, host_tools=["Read", "Edit"])
    assert runtime not in called
    host = next(row for row in rows if row["runtime"] == runtime)
    assert host["available"] and host["tools"] == ["Read", "Edit"]
    assert host["policy"]


@pytest.mark.parametrize("owner,target", [(owner, target) for owner in backends.RUNTIMES
                                          for target in backends.RUNTIMES])
def test_restricted_candidate_never_checks_other_service_config_or_auth(monkeypatch, tmp_path, owner, target):
    authenticated = []
    configured = []
    original = backends.role_config

    def config(runtime, *args):
        configured.append(runtime)
        return original(runtime, *args)

    monkeypatch.setattr(backends, "role_config", config)
    monkeypatch.setattr(backends, "auth", lambda runtime, *args: authenticated.append(runtime))
    rows = backends.candidates({}, str(tmp_path), "implement", native=owner,
                               host_tools=["Read"], allowed_runtimes=[target])
    assert [row["runtime"] for row in rows] == [target]
    assert configured == [target]
    assert authenticated == ([] if owner == target else [target])


@pytest.mark.parametrize("runtime", backends.RUNTIMES)
def test_missing_custom_role_is_not_silently_replaced(tmp_path, runtime):
    with pytest.raises(ValueError, match="custom"):
        backends.role_config(runtime, "review", {"projectRoles": {"review": "custom-review"}})


def test_private_custom_role_definition_and_model_policy(tmp_path):
    source = tmp_path / "review.toml"
    source.write_text('model = "sample-model"\nmodel_reasoning_effort = "high"\ndeveloper_instructions = "Read policy.txt"')
    config = backends.role_config("codex", "review", {"projectRoles": {"review": "custom-review"},
        "roleDefinitions": {"codex": {"custom-review": str(source)}}})
    assert config["model"] == "sample-model"
    assert "Read policy.txt" in config["instructions"]


@pytest.mark.parametrize("runtime", backends.RUNTIMES)
def test_stream_without_terminal_is_failure(runtime):
    assert not backends.parse_result(runtime, result('{"type":"message"}'), "dev-medium")["ok"]


def test_codex_error_after_message_is_failure():
    rows = [{"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.failed"}]
    assert not backends.parse_result("codex", result("\n".join(map(json.dumps, rows))), "dev-medium")["ok"]


def test_grok_stream_chunks_and_completion():
    rows = [{"params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "observed"}}}},
            {"params": {"update": {"sessionUpdate": "turn_completed"}}}]
    parsed = backends.parse_result("grok", result("\n".join(map(json.dumps, rows))), "researcher")
    assert parsed["ok"] and parsed["result"] == "observed"


def test_grok_headless_text_and_end_contract():
    rows = [{"type": "text", "data": "pre-tool explanation"}, {"type": "tool_call", "toolName": "read_file"},
            {"type": "text", "data": "observed"}, {"type": "end", "stopReason": "end_turn", "sessionId": "fixture-session"}]
    parsed = backends.parse_result("grok", result("\n".join(map(json.dumps, rows))), "researcher")
    assert parsed["ok"] and parsed["result"] == "observed" and parsed["session_id"] == "fixture-session"


@pytest.mark.parametrize("runtime", backends.RUNTIMES)
def test_structured_quota_error_is_classified_without_guessing_reset(runtime):
    event = {"type": "result" if runtime != "codex" else "turn.failed", "is_error": True,
             "subtype": "error_during_execution", "error": {"code": "rate_limit_exceeded"}}
    parsed = backends.parse_result(runtime, result(json.dumps(event)), "dev-medium")
    assert not parsed["ok"] and parsed["errorCode"] == "rate_limited"
    assert "retryAfter" not in parsed


@pytest.mark.parametrize("runtime", backends.RUNTIMES)
def test_commands_do_not_bypass_permissions_and_keep_argument_boundaries(tmp_path, runtime):
    config = backends.role_config(runtime, "dev-medium")
    config["session"] = "00000000-0000-0000-0000-000000000001"
    config["instructions"] += ' a "quoted" instruction'
    argv = backends.command(runtime, "/fictional/cli", "dev-medium", config,
                            str(tmp_path), tmp_path, {})
    assert not any(value in argv for value in ("--always-approve", "--dangerously-skip-permissions", "bypassPermissions"))
    assert argv[0] == "/fictional/cli"
    if runtime == "grok":
        assert argv[argv.index("--rules") + 1] == config["instructions"]


@pytest.mark.parametrize("model", ["inherit", "", "fictional-review-model"])
def test_claude_external_review_preserves_inheritance_and_exact_read_tools(tmp_path, model):
    config = backends.role_config("claude", "code-reviewer")
    config.update(model=model, session="00000000-0000-0000-0000-000000000001")
    argv = backends.command("claude", "/fictional/cli", "code-reviewer", config,
                            str(tmp_path), tmp_path, {})
    if model in ("inherit", ""):
        assert "--model" not in argv
    else:
        assert argv[argv.index("--model") + 1] == model
    assert argv[argv.index("--effort") + 1] == config["effort"]
    for flag in ("--tools", "--allowedTools"):
        assert argv[argv.index(flag) + 1] == "Read,Grep,Glob,Bash"
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"] == ["Read", "Grep", "Glob", "Bash"]


def test_subscription_credentials_only_and_parent_markers_removed(monkeypatch, tmp_path):
    for name in ("XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CHEF_HERMES_WORKER", "GROK_SESSION_ID"):
        monkeypatch.setenv(name, "fictional-value")
    env = backends.environment("grok", str(tmp_path))
    assert all(name not in env for name in ("XAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CHEF_HERMES_WORKER", "GROK_SESSION_ID"))


def test_grok_auth_explicitly_requires_cached_token(monkeypatch, tmp_path):
    monkeypatch.setattr(backends.shutil, "which", lambda _: "/fictional/grok")
    def run(argv, prompt, **kwargs):
        rows = [json.loads(line) for line in prompt.splitlines()]
        assert rows[1]["params"]["methodId"] == "cached_token"
        return result('{"id":2,"error":{"message":"expired"}}')
    monkeypatch.setattr(backends.process, "run", run)
    with pytest.raises(ValueError, match="cached subscription"):
        backends.auth("grok", str(tmp_path))


def test_grok_native_permissions_cannot_reenable_write_or_shell_tools(tmp_path):
    config = {**backends.role_config("grok", "implement"), "session": "00000000-0000-0000-0000-000000000001"}
    argv = backends.command("grok", "/fictional/grok", "dev-medium", config, str(tmp_path), tmp_path, {})
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    removed = argv[argv.index("--disallowed-tools") + 1].split(",")
    assert all(tool in removed for tool in ("Bash", "PowerShell", "Write", "Edit", "spawn_subagent"))
    assert argv[argv.index("--deny") + 1] == "MCPTool(*)"


@pytest.mark.parametrize("owner_runtime,target", [(owner, target) for owner in backends.RUNTIMES
                                                 for target in backends.RUNTIMES if owner != target])
def test_six_cross_runtime_paths_preserve_owner_and_collect_success(tmp_path, monkeypatch, owner_runtime, target):
    import os
    from chef.guards import mission
    from chef.workers import edits

    state = tmp_path / "state"
    state.mkdir()
    owner = {"runtime": owner_runtime, "session": "fictional-owner", "mission": "fictional-mission",
             "state": str(state), "files": ["assigned.txt"], "cancelPath": str(tmp_path / "parent-cancel.json")}
    (state / "ACTIVE").write_text(json.dumps({"id": owner["mission"], "session": owner["session"], "runtime": owner_runtime}))
    (tmp_path / "assigned.txt").write_text("before")
    captured = []
    monkeypatch.setattr(backends, "auth", lambda *args: "/fictional/" + target)
    monkeypatch.setattr(mission, "decide", lambda _: SimpleNamespace(deny=""))
    def run(argv, prompt, **kwargs):
        data = json.loads(Path(kwargs["env"][backends.context.ENV]).read_text())
        assert data["owner"] == owner and data["backend"] == target and data["pid"] == os.getpid()
        assert data["cancelPath"] == owner["cancelPath"]
        captured.append(data)
        Path(data["ready"]).write_text(data["digest"])
        Path(data["guardReady"]).write_text(data["digest"])
        text = "done"
        if target == "grok" and data["phase"] == "work":
            text = json.dumps({"summary": "changed", "changes": [{"path": "assigned.txt",
                "beforeSha256": edits.file_hash(tmp_path / "assigned.txt"), "content": "after"}]})
        if target == "codex":
            rows = [{"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000001"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                    {"type": "turn.completed"}]
        else:
            rows = [{"type": "result", "subtype": "success", "is_error": False, "result": text}]
        return result("\n".join(map(json.dumps, rows)))
    monkeypatch.setattr(backends.process, "run", run)
    response = backends.run(target, "implement", "fictional task", cwd=str(tmp_path), owner=owner,
                            context_dir=tmp_path / "attempt", timeout=10)
    assert response["ok"] and len(captured) == 2
    assert captured[0]["phase"] == "probe" and captured[1]["phase"] == "work"
    assert json.loads((state / "ACTIVE").read_text())["runtime"] == owner_runtime
    if target == "grok":
        assert (tmp_path / "assigned.txt").read_text() == "after"
        assert Path(captured[0]["ready"]).parent != tmp_path / "attempt"
        assert (tmp_path / "attempt/apply-journal.json").is_file()
