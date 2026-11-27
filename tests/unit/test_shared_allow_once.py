"""Invented guard participants exercise the common one-invocation broker."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chef.guards import allow_once as broker
from chef.guards import mission
from chef.guards.contracts import Active
from tests.paths import PLUGIN_ROOT


@pytest.fixture
def invocation(tmp_path):
    active = Active("m-demo", "owner-demo", "2026-01-01T00:00:00Z", runtime="claude")
    state = tmp_path / "state"
    mission.write_active(state, active)
    folder = state / active.id
    folder.mkdir()
    command = "widget publish --draft 17"
    payload = {
        "han_runtime": "claude", "session_id": active.session,
        "cwd": str(tmp_path), "tool_name": "Bash", "tool_use_id": "call-demo",
        "tool_input": {"command": command},
    }
    config = {"guard": {"allowOnceParticipants": ["chef-mission", "alpha"]}}
    (folder / "allow-once").write_bytes((command + "\n").encode())
    return state, active, payload, config


def use(invocation, participant="chef-mission", **options):
    state, active, payload, config = invocation
    return broker.consume_shared_allow_once(
        state, active.id, options.pop("command", payload["tool_input"]["command"]),
        payload=options.pop("payload", payload), participant=participant,
        participants=options.pop("participants", config["guard"]["allowOnceParticipants"]),
        active=options.pop("active", active), config=options.pop("config", config), **options,
    )


def token(invocation):
    return invocation[0] / invocation[1].id / "allow-once"


@pytest.mark.parametrize("first,second", [("chef-mission", "alpha"), ("alpha", "chef-mission")])
def test_each_participant_acknowledges_once_in_either_order(invocation, first, second):
    assert use(invocation, first)
    assert token(invocation).read_bytes() == b""
    assert use(invocation, second)
    with pytest.raises(broker.SharedAllowOnceError, match="already consumed"):
        use(invocation, first)
    with pytest.raises(broker.SharedAllowOnceError, match="already consumed"):
        use(invocation, second)


def test_preserves_unconsumed_lines_exactly(invocation):
    command = invocation[2]["tool_input"]["command"].encode()
    original = b"\xef\xbb\xbf  widget inspect\r\n\r\n" + command + b"\nwidget status  \r\n"
    token(invocation).write_bytes(original)
    assert use(invocation)
    assert token(invocation).read_bytes() == original.replace(command + b"\n", b"", 1)


def test_first_line_preserves_bom_on_remaining_document(invocation):
    command = invocation[2]["tool_input"]["command"].encode()
    token(invocation).write_bytes(b"\xef\xbb\xbf" + command + b"\r\n widget inspect\r\n")
    assert use(invocation)
    assert token(invocation).read_bytes() == b"\xef\xbb\xbf widget inspect\r\n"


@pytest.mark.parametrize("key", ["tool_use_id", "tool_call_id", "call_id"])
def test_native_id_aliases(invocation, key):
    payload = invocation[2]
    payload[key] = payload.pop("tool_use_id")
    assert use(invocation)


@pytest.mark.parametrize("case", ["missing", "conflict", "empty", "owner", "agent", "worker", "runtime", "active"])
def test_unsafe_identity_never_consumes(invocation, case):
    payload = invocation[2]
    before = token(invocation).read_bytes()
    options = {}
    if case == "missing":
        payload.pop("tool_use_id")
    elif case == "conflict":
        payload["call_id"] = "different-id"
    elif case == "empty":
        payload["tool_use_id"] = ""
    elif case == "owner":
        payload["session_id"] = "another-owner"
    elif case == "agent":
        payload["agent_id"] = "child"
    elif case == "worker":
        payload["han_worker"] = True
    elif case == "runtime":
        payload["han_runtime"] = "grok"
    else:
        mission.write_active(invocation[0], invocation[1]._replace(opened_at="later"))
    with pytest.raises(broker.SharedAllowOnceError):
        use(invocation, **options)
    assert token(invocation).read_bytes() == before


@pytest.mark.parametrize("field", ["command", "cwd", "shell", "config"])
def test_receipt_conflict_denies_even_with_new_approval_line(invocation, field):
    assert use(invocation)
    state, active, payload, config = invocation
    if field == "command":
        payload["tool_input"]["command"] = "widget publish --draft 18"
    elif field == "cwd":
        payload["tool_input"]["workdir"] = str(state)
    elif field == "shell":
        payload["tool_name"] = "PowerShell"
    else:
        config["feature"] = "changed"
    token(invocation).write_text(payload["tool_input"]["command"] + "\n", encoding="utf-8")
    before = token(invocation).read_bytes()
    with pytest.raises(broker.SharedAllowOnceError, match="context changed"):
        use(invocation, "alpha")
    assert token(invocation).read_bytes() == before


def test_new_call_id_cannot_reuse_receipt(invocation):
    assert use(invocation)
    invocation[2]["tool_use_id"] = "next-call"
    assert use(invocation, "alpha") is False


@pytest.mark.parametrize("command", ["widget inspect; widget inspect", "echo han-mission close m-demo", "env --chdir other widget inspect"])
def test_compound_marker_and_uninspectable_wrapper_fail_closed(invocation, command):
    invocation[2]["tool_input"]["command"] = command
    token(invocation).write_text(command + "\n", encoding="utf-8")
    with pytest.raises(broker.SharedAllowOnceError):
        use(invocation)


def test_multiline_never_consumes_individual_approval_lines(invocation):
    command = "widget inspect\nwidget status"
    invocation[2]["tool_input"]["command"] = command
    token(invocation).write_text(command + "\n", encoding="utf-8")
    before = token(invocation).read_bytes()
    assert use(invocation) is False
    assert token(invocation).read_bytes() == before


def test_receipt_publish_failure_does_not_restore_consumed_approval(invocation, monkeypatch):
    def failed(*_args, **_kwargs):
        raise OSError("fixture publication failure")

    monkeypatch.setattr(broker, "atomic_write_json", failed)
    with pytest.raises(broker.SharedAllowOnceError, match="fixture publication"):
        use(invocation)
    assert token(invocation).read_bytes() == b""
    assert use(invocation, "alpha") is False


def test_concurrent_distinct_participants_share_only_one_token(invocation):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: use(invocation, name), ["chef-mission", "alpha"]))
    assert results == [True, True]
    assert token(invocation).read_bytes() == b""
    assert len(list((token(invocation).parent / "allow-once.receipts").glob("*.json"))) == 1


def test_concurrent_same_participant_has_one_winner(invocation):
    def attempt(_):
        try:
            return use(invocation)
        except broker.SharedAllowOnceError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [False, True]


def test_unrelated_normal_commands_need_no_approval_or_id(invocation):
    assert use(invocation)
    payload = invocation[2]
    payload.pop("tool_use_id")
    payload["tool_input"]["command"] = "widget inspect; widget status"
    assert use(invocation, "alpha") is False
    payload["tool_use_id"] = "ordinary-call"
    assert use(invocation, "alpha") is False


def test_native_exec_and_powershell_have_same_shell_context(invocation):
    state, active, payload, config = invocation
    active = active._replace(runtime="codex")
    mission.write_active(state, active)
    fixture = state, active, payload, config
    payload.update(han_runtime="codex", tool_name="exec_command")
    payload["tool_input"]["shell"] = "pwsh.exe"
    assert use(fixture)
    payload["tool_name"] = "PowerShell"
    assert use(fixture, "alpha")


def test_command_alias_must_match_supplied_command(invocation):
    invocation[2]["tool_input"]["cmd"] = "widget inspect"
    with pytest.raises(broker.SharedAllowOnceError, match="conflicting command"):
        use(invocation)


@pytest.mark.parametrize("shell", ["cmd.exe", "fish", "unknown-shell", 7])
def test_unsupported_or_conflicting_shell_cannot_consume(invocation, shell):
    invocation[2]["tool_input"]["shell"] = shell
    before = token(invocation).read_bytes()
    with pytest.raises(broker.SharedAllowOnceError):
        use(invocation)
    assert token(invocation).read_bytes() == before


@pytest.mark.parametrize("participants", [[], ["chef-mission"], ["alpha", "beta"], ["chef-mission", "alpha", "alpha"], "alpha"])
def test_malformed_opt_in_denies(invocation, participants):
    with pytest.raises(broker.SharedAllowOnceError):
        use(invocation, participants=participants)


def test_adapter_context_is_deep_canonical_copy(invocation):
    config = invocation[3]
    copied = broker.shared_once_context(config)
    assert copied["adapter"] == config and copied["adapter"] is not config
    copied["adapter"]["guard"]["allowOnceParticipants"].append("beta")
    assert copied["adapter"] != config


def test_authority_bytes_bound_to_receipt(invocation):
    state, active, payload, config = invocation
    authority = state / "policy.json"
    authority.write_text('{"policy":1}', encoding="utf-8")
    config["guard"]["allowOnceAuthorityFiles"] = [str(authority)]
    assert use(invocation, config=broker.shared_once_context(config))
    authority.write_text('{"policy":2}', encoding="utf-8")
    with pytest.raises(broker.SharedAllowOnceError, match="context changed"):
        use(invocation, "alpha", config=broker.shared_once_context(config))


@pytest.mark.parametrize("files", [["relative.json"], ["/missing-policy.json"], ["/one"] * 9, "policy"])
def test_invalid_authority_files_fail_closed(files):
    with pytest.raises(broker.SharedAllowOnceError):
        broker.shared_once_context({"guard": {"allowOnceAuthorityFiles": files}})


def test_duplicate_authority_files_fail_closed(tmp_path):
    authority = tmp_path / "policy.json"
    authority.write_text("{}", encoding="utf-8")
    with pytest.raises(broker.SharedAllowOnceError, match="duplicate"):
        broker.shared_once_context({"guard": {"allowOnceAuthorityFiles": [str(authority), str(authority)]}})


@pytest.mark.parametrize("field", ["cwd", "workdir"])
def test_relative_directory_normalization_is_idempotent_and_keeps_owner_context(invocation, field):
    payload = invocation[2]
    payload["tool_input"][field] = "nested"
    first = broker.normalize_shared_payload(payload)
    second = broker.normalize_shared_payload(first)
    resolved = mission.question_write_guard.resolved_payload(first)
    assert first == second == resolved
    assert first["cwd"] == os.path.normcase(str(Path(payload["cwd"]) / "nested"))
    assert first["han_origin_cwd"] == payload["cwd"]
    assert "cwd" not in first["tool_input"]


@pytest.mark.parametrize("broken", [[], {"context": {}, "participants": []}])
def test_malformed_receipt_fails_closed(invocation, broken):
    assert use(invocation)
    receipt = next((token(invocation).parent / "allow-once.receipts").glob("*.json"))
    receipt.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(broker.SharedAllowOnceError):
        use(invocation, "alpha")


@pytest.mark.parametrize("names,expected", [(["chef-mission", "alpha"], [True, True]), (["alpha", "alpha"], [False, True])])
def test_independent_hook_processes_share_kernel_lock(invocation, names, expected):
    state, active, payload, config = invocation
    start = state / "start-fixture"
    script = """
import json, sys, time
from pathlib import Path
from chef.guards import allow_once as broker, mission
state, participant, payload, config, start = json.loads(sys.argv[1])
deadline = time.monotonic() + 10
while not Path(start).exists():
    if time.monotonic() > deadline:
        raise TimeoutError('fixture start')
    time.sleep(0.01)
try:
    result = broker.consume_shared_allow_once(state, 'm-demo', payload['tool_input']['command'], payload=payload,
        participant=participant, participants=config['guard']['allowOnceParticipants'], active=mission.read_active(state), config=config)
except broker.SharedAllowOnceError:
    result = False
print(json.dumps(result))
"""
    env = {**os.environ, "PYTHONPATH": str(PLUGIN_ROOT)}
    processes = [subprocess.Popen(
        [sys.executable, "-B", "-c", script, json.dumps([str(state), name, payload, config, str(start)])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    ) for name in names]
    start.touch()
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr.decode()
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert sorted(results) == expected
    assert token(invocation).read_bytes() == b""
