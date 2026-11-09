import json
import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mission_entry
import mission_guard


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(registry))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    script = tmp_path / "extension.py"
    script.write_text("import json,sys\nrequest=json.load(sys.stdin)\nprint(json.dumps({'schema':1,'action':'continue'}))\n", encoding="utf-8")
    adapter = root / ".claude/orchestrator/adapter.json"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(json.dumps({"schema": 1, "paths": {"stateDir": ".missions"}}))
    return {"root": root, "registry": registry, "script": script, "adapter": adapter}


def register(project, **extra):
    event = {"id": "sample", "roots": [str(project["root"])], "trusted": True,
             "command": [sys.executable, str(project["script"])], "watchFiles": [str(project["script"])], **extra}
    path = project["registry"] / "mission-entry.json"
    path.write_text(json.dumps({"schema": 1, "events": [event]}), encoding="utf-8")
    return path


def enter(project, mode="new", session="session-a", mission="sample-mission", runtime="claude"):
    return mission_entry.enter(str(project["root"]), runtime, mode, session, mission, "Create a sample tool")


def marker(project, action="open", mission="sample-mission", session="session-a", runtime="claude", cwd=""):
    return mission_guard.decide({"tool_name": "Bash", "tool_input": {"command": f"echo han-mission {action} {mission}"},
                                 "cwd": cwd or str(project["root"]), "session_id": session, "han_runtime": runtime})


def test_unregistered_existing_marker_compatibility(project):
    result = enter(project)
    assert result["action"] == "continue" and "receipt" not in result
    assert not marker(project).deny


def test_registered_missing_adapter_rejects_direct_marker(project):
    project["adapter"].unlink()
    register(project)
    assert marker(project).deny
    assert not (project["root"] / ".missions/ACTIVE").exists()


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_entry_receipt_all_runtime_markers(project, runtime):
    register(project)
    result = enter(project, runtime=runtime)
    assert Path(result["receipt"]).is_file()
    assert not marker(project, runtime=runtime).deny
    active = mission_guard.read_active(str(project["root"] / ".missions"))
    assert active.session == "session-a" and active.runtime == runtime


def test_bootstrap_adapter_before_discovery(project):
    project["adapter"].unlink()
    project["script"].write_text(
        "import json,pathlib,sys\nrequest=json.load(sys.stdin)\n"
        "path=pathlib.Path(request['cwd'])/'.claude/orchestrator/adapter.json'\n"
        "path.write_text(json.dumps({'schema':1,'paths':{'stateDir':'.missions'}}))\n"
        "print(json.dumps({'schema':1,'action':'continue','project':request['cwd'],'adapter':str(path)}))\n")
    register(project)
    assert enter(project)["action"] == "continue"
    assert not marker(project).deny


@pytest.mark.parametrize("change", ["session", "mission", "runtime", "cwd", "stale", "source", "config", "adapter", "local", "route"])
def test_receipt_binds_invocation_and_inputs(project, change):
    registration = register(project)
    result = enter(project)
    kwargs = {}
    if change == "session":
        kwargs["session"] = "different-session"
    elif change == "mission":
        kwargs["mission"] = "different-mission"
    elif change == "runtime":
        kwargs["runtime"] = "codex"
    elif change == "cwd":
        child = project["root"] / "child"
        child.mkdir()
        kwargs["cwd"] = str(child)
    elif change == "stale":
        receipt = json.loads(Path(result["receipt"]).read_text())
        receipt["createdAt"] -= 601
        Path(result["receipt"]).write_text(json.dumps(receipt))
    elif change == "source":
        with project["script"].open("a") as stream:
            stream.write("\nprint('changed')\n")
    elif change == "config":
        data = json.loads(registration.read_text())
        data["events"][0]["timeoutSeconds"] = 30
        registration.write_text(json.dumps(data))
    elif change == "adapter":
        data = json.loads(project["adapter"].read_text())
        data["project"] = "fictional-workspace"
        project["adapter"].write_text(json.dumps(data))
    elif change == "local":
        project["adapter"].with_name("local.json").write_text(json.dumps({"project": "fictional-workspace"}))
    elif change == "route":
        project["adapter"].unlink()
    outcome = marker(project, **kwargs)
    assert outcome.deny
    assert not (project["root"] / ".missions/ACTIVE").exists()


def test_resume_requires_separate_receipt_and_preserves_active_until_marker(project):
    register(project)
    enter(project)
    assert not marker(project).deny
    active = (project["root"] / ".missions/ACTIVE").read_bytes()
    assert marker(project, action="resume").deny
    enter(project, mode="resume")
    assert (project["root"] / ".missions/ACTIVE").read_bytes() == active
    assert not marker(project, action="resume").deny


def test_registered_resume_rejects_another_session_before_callback(project, monkeypatch):
    register(project)
    enter(project)
    assert not marker(project).deny
    original = (project["root"] / ".missions/ACTIVE").read_bytes()
    monkeypatch.setattr(mission_entry, "execute_event", lambda *_: pytest.fail("callback ran for another session"))
    with pytest.raises(mission_entry.EntryError, match="preserve the ACTIVE session"):
        enter(project, mode="resume", session="session-b")
    assert (project["root"] / ".missions/ACTIVE").read_bytes() == original


def test_registered_resume_guard_rejects_another_session_with_matching_receipt_snapshot(project):
    register(project)
    enter(project)
    assert not marker(project).deny
    result = enter(project, mode="resume")
    data = json.loads(Path(result["receipt"]).read_text())
    data["binding"]["sessionId"] = "session-b"
    selected = mission_entry.registration(str(project["root"]), "claude")
    other_receipt = mission_entry.receipt_path(selected, data["binding"])
    Path(other_receipt).write_text(json.dumps(data))
    outcome = marker(project, action="resume", session="session-b")
    assert "preserve the ACTIVE session" in outcome.deny
    assert mission_guard.read_active(str(project["root"] / ".missions")).session == "session-a"


def test_unregistered_resume_retains_legacy_session_handoff(project):
    assert not marker(project).deny
    result = enter(project, mode="resume", session="session-b")
    assert result["action"] == "continue" and "receipt" not in result
    assert not marker(project, action="resume", session="session-b").deny
    assert mission_guard.read_active(str(project["root"] / ".missions")).session == "session-b"


def test_registration_only_applies_to_matching_root(project, tmp_path):
    register(project, roots=[str(tmp_path / "somewhere-else")])
    assert not marker(project).deny


@pytest.mark.parametrize("extra", [{"trusted": False}, {"command": "python extension.py"}, {"timeoutSeconds": 0}, {"watchFiles": []}])
def test_invalid_or_untrusted_registration_fails_closed(project, extra):
    register(project, **extra)
    with pytest.raises(mission_entry.EntryError):
        enter(project)
    assert marker(project).deny


@pytest.mark.parametrize("body", ["raise SystemExit(7)", "print('not-json')", "print('{}')", "print('{\"schema\":1,\"action\":\"unknown\"}')"])
def test_callback_failure_never_opens_active(project, body):
    project["script"].write_text(body)
    register(project)
    with pytest.raises(mission_entry.EntryError):
        enter(project)
    assert not (project["root"] / ".missions/ACTIVE").exists()
    assert not list(project["registry"].rglob("mission-entry-receipts/*.json"))


def test_callback_timeout(project):
    project["script"].write_text("import time\ntime.sleep(5)\n")
    register(project, timeoutSeconds=1)
    with pytest.raises(mission_entry.EntryError, match="timed out"):
        enter(project)


def test_failed_callback_removes_previous_receipt(project, monkeypatch):
    register(project)
    result = enter(project)
    monkeypatch.setattr(mission_entry, "execute_event", lambda *_: (_ for _ in ()).throw(mission_entry.EntryError("failed")))
    with pytest.raises(mission_entry.EntryError):
        enter(project)
    assert not Path(result["receipt"]).exists()


def test_needs_input_returns_without_receipt(project):
    project["script"].write_text("print('{\"schema\":1,\"action\":\"needs-input\",\"message\":\"Choose an option\"}')")
    register(project)
    assert enter(project) == {"schema": 1, "action": "needs-input", "message": "Choose an option"}
    assert marker(project).deny


def test_continue_cannot_override_discovered_adapter(project, tmp_path):
    other = tmp_path / "unregistered.json"
    other.write_text(project["adapter"].read_text())
    project["script"].write_text(f"import json\nprint(json.dumps({{'schema':1,'action':'continue','adapter':{str(other)!r}}}))")
    register(project)
    with pytest.raises(mission_entry.EntryError, match="disagrees"):
        enter(project)


def test_continue_must_remain_in_allowed_roots(project, tmp_path):
    register(project, allowedRoots=[str(tmp_path / "different")])
    with pytest.raises(mission_entry.EntryError, match="allowedRoots"):
        enter(project)


def test_guard_never_executes_callback(project, monkeypatch):
    register(project)
    enter(project)
    monkeypatch.setattr(mission_entry.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("hook ran an external command"))
    assert not marker(project).deny


def slot_setup(project, tmp_path):
    sys.path.insert(0, str(Path(mission_entry.DEPLOY_ROOT) / "skills/slots/scripts"))
    import slots

    slot_root = tmp_path / "slots"
    slot = slot_root / "a"
    slot.mkdir(parents=True)
    data = json.loads(project["adapter"].read_text())
    data["slots"] = {"enabled": True, "root": str(slot_root), "client": "sample_{name}", "names": ["a"], "prepare": []}
    project["adapter"].write_text(json.dumps(data))
    registry = [{"adapter": "sample", "roots": [str(slot)]}]
    (project["registry"] / "projects.json").write_text(json.dumps(registry))
    target = project["registry"] / "sample/adapter.json"
    target.parent.mkdir()
    target.write_text(json.dumps({**data, "vars": {"main": str(project["root"])}}))
    located = mission_guard.Located(str(project["root"]), str(project["adapter"]))
    config = slots.Config(located, data)
    slots.Rows(config.rows_path).upsert("a", path=str(slot), client="sample_a", created="", synced="", state="ready")
    return slots, config, slot


def delegate_script(project):
    project["script"].write_text("import json\nprint(json.dumps(" + repr({"schema": 1, "action": "delegate", "project": str(project["root"]), "slot": "a"}) + "))")


def test_delegate_runs_canonical_before_run_under_lock_without_parent_active(project, tmp_path, monkeypatch):
    slots, config, slot = slot_setup(project, tmp_path)
    helper = tmp_path / "prepare.py"
    proof = tmp_path / "proof.json"
    helper.write_text("import json,pathlib,sys\nlock=pathlib.Path(sys.argv[2])\n"
                      "pathlib.Path(sys.argv[1]).write_text(json.dumps({'locked':lock.is_file(),'owner':lock.read_text().split('\\t')[0]==sys.argv[3]}))\n")
    data = json.loads(project["adapter"].read_text())
    data["slots"]["beforeRun"] = [{"id": "prepare", "run": f'"{sys.executable}" "{helper}" "{proof}" "{{slotLockPath}}" "{{slotLockOwnerPid}}"'}]
    project["adapter"].write_text(json.dumps(data))
    delegate_script(project)
    register(project, allowedRoots=[str(project["root"]), str(slot)])
    calls = []
    monkeypatch.setattr(slots, "launch_mission", lambda config, name, command: calls.append((config.runtime, name, command, Path(config.slot_lock_path(name)).is_file())))
    result = enter(project)
    assert result["action"] == "delegate" and result["launched"]
    assert len(calls) == 1 and calls[0][3]
    assert json.loads(proof.read_text()) == {"locked": True, "owner": True}
    assert not (project["root"] / ".missions/ACTIVE").exists()
    assert marker(project).deny


def test_delegate_failure_never_launches_cli(project, tmp_path, monkeypatch):
    slots, config, slot = slot_setup(project, tmp_path)
    data = json.loads(project["adapter"].read_text())
    data["slots"]["beforeRun"] = [{"id": "prepare", "run": f'"{sys.executable}" -c "raise SystemExit(7)"'}]
    project["adapter"].write_text(json.dumps(data))
    delegate_script(project)
    register(project, allowedRoots=[str(project["root"]), str(slot)])
    monkeypatch.setattr(slots, "launch_mission", lambda *_: pytest.fail("launched after failing beforeRun"))
    with pytest.raises(mission_entry.EntryError, match="delegation failed"):
        enter(project)
    assert not (project["root"] / ".missions/ACTIVE").exists()


def test_delegate_target_outside_allowed_roots_is_rejected(project, tmp_path, monkeypatch):
    slots, config, slot = slot_setup(project, tmp_path)
    delegate_script(project)
    register(project)
    monkeypatch.setattr(slots, "launch_mission", lambda *_: pytest.fail("launched outside allowedRoots"))
    with pytest.raises(mission_entry.EntryError, match="allowedRoots"):
        enter(project)


def test_slot_reentry_cannot_delegate_again(project, tmp_path, monkeypatch):
    slots, config, slot = slot_setup(project, tmp_path)
    delegate_script(project)
    register(project, roots=[str(project["root"]), str(slot)], allowedRoots=[str(project["root"]), str(slot)])
    monkeypatch.setattr(slots, "launch_mission", lambda *_: pytest.fail("reentry delegated"))
    with pytest.raises(mission_entry.EntryError, match="cannot delegate"):
        mission_entry.enter(str(slot), "claude", "new", "slot-session", "sample-mission", "Create a sample tool")


def test_slot_reentry_continues_with_separate_receipt(project, tmp_path):
    slots, config, slot = slot_setup(project, tmp_path)
    register(project, roots=[str(slot)], allowedRoots=[str(slot)])
    result = mission_entry.enter(str(slot), "claude", "new", "slot-session", "sample-mission")
    assert result["action"] == "continue"
    assert not marker(project, session="slot-session", cwd=str(slot)).deny


def test_unwatched_command_script_is_rejected(project, tmp_path):
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("sample")
    register(project, watchFiles=[str(unrelated)])
    with pytest.raises(mission_entry.EntryError, match="watchFiles"):
        enter(project)


def test_mixed_marker_cannot_bypass_missing_adapter_receipt(project):
    project["adapter"].unlink()
    register(project)
    outcome = mission_guard.decide({"tool_name": "Bash", "tool_input": {"command": "echo han-mission open sample-mission; echo other"},
                                    "cwd": str(project["root"]), "session_id": "session-a"})
    assert outcome.deny


@pytest.mark.parametrize("saved_runtime", ["codex", "claude"])
def test_delegate_resume_preserves_recorded_session_and_runtime(project, tmp_path, monkeypatch, saved_runtime):
    slots, config, slot = slot_setup(project, tmp_path)
    session = "51c1973e-0a29-47b2-9733-660cf63fb9bc"
    mission_guard.write_active(config.slot_state_dir("a"), mission_guard.Active("sample-mission", session, mission_guard.now_iso()))
    original = Path(config.slot_state_dir("a"), "ACTIVE").read_bytes()
    delegate_script(project)
    register(project, allowedRoots=[str(project["root"]), str(slot)])
    monkeypatch.setattr(slots, "session_runtime", lambda *_args, **_kwargs: saved_runtime)
    commands = []
    monkeypatch.setattr(slots, "launch_mission", lambda config, name, command: commands.append(command))
    if saved_runtime == "codex":
        result = enter(project, mode="resume", runtime="codex")
        assert result["action"] == "delegate"
        assert len(commands) == 1
        position = commands[0].index("resume")
        assert commands[0][position + 1] == session
    else:
        with pytest.raises(mission_entry.EntryError, match="preserve the existing runtime"):
            enter(project, mode="resume", runtime="codex")
        assert not commands
    assert Path(config.slot_state_dir("a"), "ACTIVE").read_bytes() == original


def test_resume_event_cannot_rebind_adapter(project):
    register(project)
    enter(project)
    assert not marker(project).deny
    project["script"].write_text(
        "import json,pathlib,sys\nrequest=json.load(sys.stdin)\n"
        "path=pathlib.Path(request['cwd'])/'.claude/orchestrator/adapter.json'\n"
        "data=json.loads(path.read_text())\ndata['project']='changed'\npath.write_text(json.dumps(data))\n"
        "print(json.dumps({'schema':1,'action':'continue'}))\n")
    register(project)
    with pytest.raises(mission_entry.EntryError, match="preserve the existing adapter"):
        enter(project, mode="resume")


def test_callback_cannot_change_registration_during_execution(project):
    config = register(project)
    project["script"].write_text(
        f"import pathlib\npath=pathlib.Path({str(config)!r})\npath.write_text(path.read_text()+' ')\n"
        "print('{\"schema\":1,\"action\":\"continue\"}')")
    with pytest.raises(mission_entry.EntryError, match="changed while running"):
        enter(project)


@pytest.mark.parametrize("relative", ["skills/mission/scripts/mission_entry.py", "codex/skills/chef-mission/scripts/mission_entry.py", "grok/scripts/mission_entry.py"])
def test_runtime_wrapper_executes_shared_entry(project, relative):
    register(project)
    wrapper = Path(mission_entry.DEPLOY_ROOT) / relative
    done = mission_entry.subprocess.run([sys.executable, str(wrapper), "--project", str(project["root"]), "--mode", "new",
                                        "--session", "wrapper-session", "--mission", "sample-mission"], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    response = json.loads(done.stdout)
    assert response["action"] == "continue" and Path(response["receipt"]).exists()


def test_damaged_existing_adapter_rejects_before_callback(project):
    project["adapter"].write_text("not-json")
    proof = project["root"] / "callback-ran"
    project["script"].write_text(f"from pathlib import Path\nPath({str(proof)!r}).touch()\n")
    register(project)
    with pytest.raises(mission_guard.GuardOff, match="adapter.json"):
        enter(project)
    assert not proof.exists()


def test_receipt_rejects_changed_active_ownership(project):
    register(project)
    enter(project)
    mission_guard.write_active(str(project["root"] / ".missions"), mission_guard.Active("sample-mission", "someone-else", mission_guard.now_iso()))
    assert "ACTIVE changed" in marker(project).deny


@pytest.mark.parametrize("action", ["needs-input", "continue"])
def test_wrapper_json_preserves_unicode_with_legacy_stdout_encoding(project, action):
    if action == "needs-input":
        result = {"schema": 1, "action": "needs-input", "message": "선택해 주세요 🔧"}
        project["script"].write_text("import json\nprint(json.dumps(" + repr(result) + "))\n", encoding="utf-8")
    else:
        previous_root = project["root"]
        project["root"] = previous_root.with_name("workspace-도구-🚀")
        previous_root.rename(project["root"])
        project["adapter"] = project["root"] / ".claude/orchestrator/adapter.json"
    register(project)
    wrapper = Path(mission_entry.DEPLOY_ROOT) / "codex/skills/chef-mission/scripts/mission_entry.py"
    done = mission_entry.subprocess.run([sys.executable, str(wrapper), "--project", str(project["root"]), "--mode", "new",
                                        "--session", "wrapper-session", "--mission", "sample-mission"], capture_output=True,
                                       text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    if action == "needs-input":
        assert done.returncode == 3, done.stderr
        assert json.loads(done.stdout) == result
    else:
        assert done.returncode == 0, done.stderr
        response = json.loads(done.stdout)
        assert Path(response["project"]) == project["root"]
        assert Path(response["receipt"]).exists()


@pytest.mark.parametrize("mode", ["new", "resume"])
def test_registered_existing_active_pins_runtime_for_same_session(project, mode):
    register(project)
    enter(project)
    assert not marker(project).deny
    active = mission_guard.read_active(str(project["root"] / ".missions"))
    assert active.runtime == "claude"
    with pytest.raises(mission_entry.EntryError, match="preserve the existing runtime: claude"):
        enter(project, mode=mode, runtime="codex")


@pytest.mark.parametrize("mode,action", [("new", "open"), ("resume", "resume")])
def test_registered_existing_active_guard_rejects_foreign_runtime_with_matching_receipt(project, mode, action):
    register(project)
    enter(project)
    assert not marker(project).deny
    result = enter(project, mode=mode)
    receipt = json.loads(Path(result["receipt"]).read_text())
    receipt["binding"]["runtime"] = "codex"
    selected = mission_entry.registration(str(project["root"]), "codex")
    other = Path(mission_entry.receipt_path(selected, receipt["binding"]))
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps(receipt))
    assert "preserve the existing runtime: claude" in marker(project, action=action, runtime="codex").deny


@pytest.mark.parametrize("evidence", ["codex", "unknown"])
def test_registered_legacy_resume_uses_session_transcript_evidence(project, evidence):
    session = "b47c2c20-609b-4309-8458-7463e7de1d2f"
    state = project["root"] / ".missions"
    mission_guard.write_active(str(state), mission_guard.Active("sample-mission", session, mission_guard.now_iso()))
    register(project)
    if evidence == "codex":
        transcript = project["root"] / "sample-transcript.jsonl"
        transcript.write_text(json.dumps({"type": "session_meta"}) + "\n")
        folder = state / "sample-mission"
        folder.mkdir()
        (folder / "sessions.tsv").write_text("at\taction\tsession\ttranscript\nnow\topen\t" + session + "\t" + str(transcript) + "\n")
        result = enter(project, mode="resume", runtime="codex", session=session)
        assert result["action"] == "continue"
        assert not marker(project, action="resume", runtime="codex", session=session).deny
        assert mission_guard.read_active(str(state)).runtime == "codex"
    else:
        with pytest.raises(mission_entry.EntryError, match="cannot establish the existing runtime"):
            enter(project, mode="resume", runtime="codex", session=session)


@pytest.fixture
def ambiguous_legacy_session(project, tmp_path, monkeypatch):
    session = "44860a6b-117d-47d8-8011-2a31b9c7c6d5"
    state = project["root"] / ".missions"
    mission_guard.write_active(str(state), mission_guard.Active("sample-mission", session, mission_guard.now_iso()))
    homes = {"codex": tmp_path / "codex", "claude": tmp_path / "claude", "grok": tmp_path / "grok"}
    monkeypatch.setenv("CODEX_HOME", str(homes["codex"]))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(homes["claude"]))
    monkeypatch.setenv("GROK_HOME", str(homes["grok"]))
    register(project)
    return session, state / "sample-mission", homes


def write_fallback(homes, runtime, session, index):
    if runtime == "codex":
        path = homes[runtime] / "sessions" / str(index) / f"rollout-example-{session}.jsonl"
        content = {"type": "session_meta"}
    elif runtime == "claude":
        path = homes[runtime] / "projects" / str(index) / f"{session}.jsonl"
        content = {"type": "user"}
    else:
        path = homes[runtime] / "sessions" / str(index) / session / "updates.jsonl"
        content = {"params": {"sessionId": session, "update": {"sessionUpdate": "turn_completed"}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("first,second", [("codex", "claude"), ("codex", "codex"), ("claude", "claude"), ("grok", "grok")])
def test_registered_legacy_resume_rejects_ambiguous_fallbacks_and_keeps_default_reader(project, ambiguous_legacy_session, first, second):
    session, folder, homes = ambiguous_legacy_session
    write_fallback(homes, first, session, 1)
    write_fallback(homes, second, session, 2)
    slots = mission_entry.slot_tools()
    assert slots.session_runtime(str(folder), session) == first
    with pytest.raises(mission_entry.EntryError, match="ambiguous"):
        enter(project, mode="resume", runtime=first, session=session)


def test_registered_legacy_resume_keeps_explicit_session_path_priority(project, ambiguous_legacy_session):
    session, folder, homes = ambiguous_legacy_session
    write_fallback(homes, "codex", session, 1)
    selected = write_fallback(homes, "claude", session, 2)
    folder.mkdir()
    (folder / "sessions.tsv").write_text("at\taction\tsession\ttranscript\nnow\topen\t" + session + "\t" + str(selected) + "\n")
    result = enter(project, mode="resume", runtime="claude", session=session)
    assert result["action"] == "continue"
    assert not marker(project, action="resume", runtime="claude", session=session).deny


@pytest.fixture
def deployment_root(tmp_path, monkeypatch):
    tools = mission_entry.slot_tools()
    root = tmp_path / "installed-plugin"
    root.mkdir()
    monkeypatch.setattr(mission_entry, "slot_tools", lambda: tools)
    monkeypatch.setattr(mission_entry, "DEPLOY_ROOT", str(root))
    return root


def test_read_only_public_watch_input_is_allowed_and_update_invalidates_receipt(project, deployment_root):
    public_input = deployment_root / "roles/example.toml"
    public_input.parent.mkdir()
    public_input.write_text('name = "example"\n')
    register(project, watchFiles=[str(project["script"]), str(public_input)])
    result = enter(project)
    assert Path(result["receipt"]).exists()
    public_input.write_text('name = "updated-example"\n')
    assert "external source changed" in marker(project).deny


@pytest.mark.parametrize("input_kind", ["executable", "script", "configuration"])
def test_command_executable_script_and_configuration_stay_outside_deployment(project, deployment_root, input_kind):
    deployed = deployment_root / "external-input"
    deployed.write_text("sample input")
    command = [sys.executable, str(project["script"])]
    if input_kind == "executable":
        command[0] = str(deployed)
    elif input_kind == "script":
        command[1] = str(deployed)
    else:
        command.extend(["--config", str(deployed)])
    register(project, command=command, watchFiles=[str(project["script"]), str(deployed)])
    with pytest.raises(mission_entry.EntryError, match="outside the installed plugin"):
        enter(project)


def test_registration_stays_outside_deployment(project, deployment_root, monkeypatch):
    register(project)
    monkeypatch.setattr(mission_entry, "DEPLOY_ROOT", str(project["registry"]))
    with pytest.raises(mission_entry.EntryError, match="registration must be outside"):
        enter(project)


def test_receipt_state_stays_outside_deployment(project, deployment_root, monkeypatch):
    register(project)
    monkeypatch.setattr(mission_entry, "DEPLOY_ROOT", str(project["registry"] / mission_entry.RECEIPTS_DIR))
    with pytest.raises(mission_entry.EntryError, match="receipts must be outside"):
        enter(project)


def test_existing_active_new_entry_cannot_change_state_binding(project):
    register(project)
    enter(project)
    assert not marker(project).deny
    original = (project["root"] / ".missions/ACTIVE").read_bytes()
    project["script"].write_text(
        "import json,pathlib,sys\nrequest=json.load(sys.stdin)\n"
        "path=pathlib.Path(request['cwd'])/'.claude/orchestrator/adapter.json'\n"
        "data=json.loads(path.read_text())\ndata['paths']['stateDir']='.other-missions'\npath.write_text(json.dumps(data))\n"
        "print(json.dumps({'schema':1,'action':'continue'}))\n")
    with pytest.raises(mission_entry.EntryError, match="preserve the existing adapter"):
        enter(project)
    assert (project["root"] / ".missions/ACTIVE").read_bytes() == original
    assert not (project["root"] / ".other-missions/ACTIVE").exists()


def test_guard_rechecks_adapter_state_binding_before_transition(project, monkeypatch):
    register(project)
    enter(project)
    original = mission_entry.guard_location
    calls = []

    def checked(*args):
        context = original(*args)
        if not calls:
            data = json.loads(project["adapter"].read_text())
            data["paths"]["stateDir"] = ".other-missions"
            project["adapter"].write_text(json.dumps(data))
        calls.append(context)
        return context

    monkeypatch.setattr(mission_entry, "guard_location", checked)
    assert marker(project).deny
    assert not (project["root"] / ".missions/ACTIVE").exists()
    assert not (project["root"] / ".other-missions/ACTIVE").exists()


def test_registered_junction_or_symlink_cwd_cannot_bypass_receipt(project, tmp_path):
    alias = tmp_path / "workspace-alias"
    if os.name == "nt":
        created = mission_entry.subprocess.run(
            ["powershell", "-NoProfile", "-Command", "New-Item -ItemType Junction -Path $env:ENTRY_TEST_LINK -Target $env:ENTRY_TEST_TARGET | Out-Null"],
            env={**os.environ, "ENTRY_TEST_LINK": str(alias), "ENTRY_TEST_TARGET": str(project["root"])}, capture_output=True)
        assert created.returncode == 0, created.stderr
    else:
        alias.symlink_to(project["root"], target_is_directory=True)
    register(project)
    assert marker(project, cwd=str(alias)).deny
    result = mission_entry.enter(str(alias), "claude", "new", "session-a", "sample-mission")
    assert result["action"] == "continue"
    assert not marker(project, cwd=str(alias)).deny


def listen():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(10)
    return listener


def finish_processes(processes):
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_registered_new_markers_serialize_same_active_across_runtimes(project, tmp_path):
    register(project)
    enter(project, runtime="claude", session="session-a")
    enter(project, runtime="codex", session="session-b")
    harness = tmp_path / "marker_harness.py"
    harness.write_text(
        "import json,socket,sys\nsys.path.insert(0,sys.argv[1])\nimport mission_entry,mission_guard\n"
        "payload=json.loads(sys.argv[2])\noriginal_check=mission_entry.guard_location\ncalls=0\n"
        "def checked(*args):\n global calls\n result=original_check(*args)\n calls+=1\n"
        " if calls==1:\n  with socket.create_connection(('127.0.0.1',int(sys.argv[3]))) as gate:\n"
        "   gate.sendall(payload['session_id'].encode())\n   gate.recv(1)\n return result\n"
        "mission_entry.guard_location=checked\noriginal_marker=mission_guard.handle_marker\n"
        "def transition(*args,**kwargs):\n with socket.create_connection(('127.0.0.1',int(sys.argv[4]))) as gate:\n"
        "  gate.sendall(payload['session_id'].encode())\n  gate.recv(1)\n return original_marker(*args,**kwargs)\n"
        "mission_guard.handle_marker=transition\nresult=mission_guard.decide(payload)\nprint(json.dumps({'deny':result.deny}))\n")
    processes = []
    gates = []
    with listen() as snapshot, listen() as transition:
        try:
            for runtime, session in [("claude", "session-a"), ("codex", "session-b")]:
                payload = {"tool_name": "Bash", "tool_input": {"command": "echo han-mission open sample-mission"},
                           "cwd": str(project["root"]), "session_id": session, "han_runtime": runtime}
                process = mission_entry.subprocess.Popen([sys.executable, str(harness), str(Path(mission_entry.__file__).parent), json.dumps(payload),
                                                         str(snapshot.getsockname()[1]), str(transition.getsockname()[1])],
                                                        stdout=mission_entry.subprocess.PIPE, stderr=mission_entry.subprocess.PIPE)
                processes.append(process)
                gate, _ = snapshot.accept()
                gate.settimeout(10)
                assert gate.recv(100).decode() == session
                gates.append(gate)
            for gate in gates:
                gate.sendall(b"x")
            holder, _ = transition.accept()
            holder.settimeout(10)
            gates.append(holder)
            winning_session = holder.recv(100).decode()
            transition.settimeout(1)
            with pytest.raises(TimeoutError):
                extra, _ = transition.accept()
                gates.append(extra)
            holder.sendall(b"x")
            outcomes = []
            for process in processes:
                out, err = process.communicate(timeout=10)
                assert process.returncode == 0, err
                outcomes.append(json.loads(out))
            assert sum(bool(outcome["deny"]) for outcome in outcomes) == 1
            assert mission_guard.read_active(str(project["root"] / ".missions")).session == winning_session
        finally:
            for gate in gates:
                gate.close()
            finish_processes(processes)


@pytest.mark.parametrize("later", ["fail", "needs-input"])
def test_overlapping_binding_entry_serializes_callback_and_failed_receipt_invalidation(project, tmp_path, later):
    harness = tmp_path / "entry_harness.py"
    harness.write_text(
        "import socket,sys\nsys.path.insert(0,sys.argv[1])\nimport mission_entry\nslots=mission_entry.slot_tools()\n"
        "original=slots.FileLock\nclass ObservedLock(original):\n def __enter__(self):\n"
        "  with socket.create_connection(('127.0.0.1',int(sys.argv[2]))) as notice:\n   notice.sendall(b'attempt')\n"
        "  return super().__enter__()\nslots.FileLock=ObservedLock\nsys.exit(mission_entry.main(sys.argv[3:]))\n")
    processes = []
    gates = []
    with listen() as notices, listen() as callbacks:
        project["script"].write_text(
            "import json,socket,sys\njson.load(sys.stdin)\n"
            f"with socket.create_connection(('127.0.0.1',{callbacks.getsockname()[1]})) as gate:\n"
            " gate.sendall(b'callback')\n decision=gate.recv(100).decode()\n"
            "if decision=='fail':\n raise SystemExit(7)\n"
            "result={'schema':1,'action':'continue'}\n"
            "if decision=='needs-input':\n result={'schema':1,'action':'needs-input','message':'Choose an option'}\n"
            "print(json.dumps(result))\n")
        register(project)
        args = [sys.executable, str(harness), str(Path(mission_entry.__file__).parent), str(notices.getsockname()[1]),
                "--project", str(project["root"]), "--mode", "new", "--session", "session-a", "--mission", "sample-mission"]
        try:
            first = mission_entry.subprocess.Popen(args, stdout=mission_entry.subprocess.PIPE, stderr=mission_entry.subprocess.PIPE)
            processes.append(first)
            notice, _ = notices.accept()
            assert notice.recv(100) == b"attempt"
            notice.close()
            initial, _ = callbacks.accept()
            initial.settimeout(10)
            assert initial.recv(100) == b"callback"
            gates.append(initial)
            second = mission_entry.subprocess.Popen(args, stdout=mission_entry.subprocess.PIPE, stderr=mission_entry.subprocess.PIPE)
            processes.append(second)
            notice, _ = notices.accept()
            assert notice.recv(100) == b"attempt"
            notice.close()
            callbacks.settimeout(1)
            with pytest.raises(TimeoutError):
                extra, _ = callbacks.accept()
                gates.append(extra)
            initial.sendall(b"continue")
            out, err = first.communicate(timeout=10)
            assert first.returncode == 0, err
            receipt = Path(json.loads(out)["receipt"])
            callbacks.settimeout(10)
            following, _ = callbacks.accept()
            following.settimeout(10)
            gates.append(following)
            assert following.recv(100) == b"callback"
            following.sendall(later.encode())
            _, err = second.communicate(timeout=10)
            if later == "fail":
                assert second.returncode == 1, err
            else:
                assert second.returncode == 3, err
            assert not receipt.exists()
        finally:
            for gate in gates:
                gate.close()
            finish_processes(processes)
