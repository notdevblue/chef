import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import doctor  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def make_registry(tmp_path, cases=True, unity=None):
    adapters = tmp_path / "adapters"
    project_adapter = adapters / "t"
    project_adapter.mkdir(parents=True)
    main = tmp_path / "main"
    (main / "state").mkdir(parents=True)
    adapter = {"schema": 1, "project": "T", "paths": {"vcsRoot": ".", "stateDir": "state"},
               "guard": {"deny": [{"pattern": "^p4 submit", "reason": "no"}]}}
    if unity:
        adapter["vars"] = {"unityExe": unity}
    (project_adapter / "adapter.json").write_text(json.dumps(adapter), encoding="utf-8")
    if cases:
        (project_adapter / "guard-cases.json").write_text(json.dumps([{"expect": "deny", "command": "p4 submit"}]), encoding="utf-8")
    (adapters / "projects.json").write_text(json.dumps([{"adapter": "t", "roots": [str(main)]}]), encoding="utf-8")
    return adapters, main


def levels(report, area):
    return [row["level"] for row in report.rows if row["area"] == area]


def test_doctor_reports_registry_adapter_cases_and_stale_active(tmp_path, monkeypatch):
    adapters, main = make_registry(tmp_path, unity=str(tmp_path / "missing.exe"))
    active = main / "state" / "ACTIVE"
    active.write_text(json.dumps({"id": "m1", "session": "s"}), encoding="utf-8")
    old = 1_000_000_000
    os.utime(active, (old, old))
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    report = doctor.run("claude")
    assert levels(report, "registry") == ["OK"]
    assert levels(report, "guard-cases") == ["OK"]
    assert levels(report, "unity") == ["FAIL"]
    stale = [row["text"] for row in report.rows if row["area"] == "active"]
    assert stale and "close-stale" in stale[0] and "--force-close-stale" in stale[0]
    assert active.exists()  # 읽기 전용


def write_role_policy(adapters, adapter_extra=None, local=None):
    path = adapters / "t" / "adapter.json"
    adapter = json.loads(path.read_text(encoding="utf-8"))
    adapter.update(adapter_extra or {})
    path.write_text(json.dumps(adapter), encoding="utf-8")
    if local is not None:
        (adapters / "t" / "local.json").write_text(json.dumps(local), encoding="utf-8")


def role_policy_rows(report):
    return [(row["level"], row["text"]) for row in report.rows if row["area"] == "role-policy"]


def test_doctor_role_policy_absent_adds_no_rows(tmp_path, monkeypatch):
    adapters, _ = make_registry(tmp_path)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    assert role_policy_rows(doctor.run("claude")) == []


def test_doctor_role_policy_valid_values_are_ok(tmp_path, monkeypatch):
    adapters, _ = make_registry(tmp_path)
    write_role_policy(adapters, {"rolePolicy": {"model": "inherit", "subagents": False},
                                 "runtimes": {"codex": {"rolePolicy": {"model": "definition"}}, "grok": {"rolePolicy": {"subagents": True}}}},
                      local={"rolePolicy": {"model": "definition"}})
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    report = doctor.run("claude")
    rows = role_policy_rows(report)
    assert [level for level, _ in rows] == ["OK"] * 4
    assert not report.failed


@pytest.mark.parametrize("policy,needle", [
    ({"model": "same"}, "model"),
    ({"model": True}, "model"),
    ({"subagents": "no"}, "subagents"),
    ({"subagents": 0}, "subagents"),
    ("inherit", "객체"),
])
def test_doctor_role_policy_bad_values_fail(tmp_path, monkeypatch, policy, needle):
    adapters, _ = make_registry(tmp_path)
    write_role_policy(adapters, {"rolePolicy": policy})
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    report = doctor.run("claude")
    rows = role_policy_rows(report)
    assert [level for level, _ in rows] == ["FAIL"] and needle in rows[0][1]
    assert report.failed


def test_doctor_role_policy_unknown_key_warns(tmp_path, monkeypatch):
    adapters, _ = make_registry(tmp_path)
    write_role_policy(adapters, {"rolePolicy": {"model": "inherit", "effort": "high"}})
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    rows = role_policy_rows(doctor.run("claude"))
    assert [level for level, _ in rows] == ["WARN"] and "effort" in rows[0][1]


@pytest.mark.parametrize("source", ["adapter", "local"])
@pytest.mark.parametrize("runtime", ["codex", "grok"])
def test_doctor_role_policy_runtime_overlay_values_are_checked(tmp_path, monkeypatch, source, runtime):
    adapters, _ = make_registry(tmp_path)
    overlay = {"runtimes": {runtime: {"rolePolicy": {"model": "other", "subagents": "x"}}}}
    if source == "adapter":
        write_role_policy(adapters, overlay)
    else:
        write_role_policy(adapters, local=overlay)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    rows = role_policy_rows(doctor.run("claude"))
    assert [level for level, _ in rows] == ["FAIL", "FAIL"]
    assert all(f"runtimes.{runtime}.rolePolicy" in text and f"{source}.json" in text for _, text in rows)


@pytest.mark.parametrize("source", ["adapter", "local"])
def test_doctor_role_policy_claude_overlay_warns_ignored(tmp_path, monkeypatch, source):
    adapters, _ = make_registry(tmp_path)
    overlay = {"runtimes": {"claude": {"rolePolicy": {"model": "inherit"}}}}
    if source == "adapter":
        write_role_policy(adapters, overlay)
    else:
        write_role_policy(adapters, local=overlay)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    report = doctor.run("claude")
    rows = role_policy_rows(report)
    assert [level for level, _ in rows] == ["WARN"]
    assert "runtimes.claude.rolePolicy" in rows[0][1] and f"{source}.json" in rows[0][1]
    assert not report.failed


def test_doctor_without_registry_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "none"))
    report = doctor.run("codex")
    assert levels(report, "registry") == ["FAIL"]


def test_doctor_grok_runtime_uses_shared_mission_script(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "check_grok", lambda report: report.add("OK", "grok-cli", "synthetic"))
    adapters, main = make_registry(tmp_path)
    active = main / "state" / "ACTIVE"
    active.write_text(json.dumps({"id": "m1", "session": "s"}), encoding="utf-8")
    old = 1_000_000_000
    os.utime(active, (old, old))
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    report = doctor.run("grok")
    assert levels(report, "registry") == ["OK"]
    assert doctor.MISSION_STATE["grok"] == doctor.MISSION_STATE["claude"]
    doctor.main(["--runtime", "grok"])
    out = capsys.readouterr().out.replace("\\", "/")
    assert 'skills/mission/scripts/mission_state.py" --runtime grok --project' in out
    assert "codex/skills" not in out
    assert active.exists()


def test_doctor_cli_json(tmp_path, monkeypatch, capsys):
    adapters, _ = make_registry(tmp_path, cases=False)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    doctor.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert any(row["area"] == "guard-cases" and row["level"] == "WARN" for row in out["rows"])


def grok_inspection(trusted=False):
    skill_root = os.path.join(doctor.PLUGIN, "grok", "skills")
    skills = [{"name": name} for name in os.listdir(skill_root)
              if name not in ("chef-watch", "chef-handoff") and os.path.isfile(os.path.join(skill_root, name, "SKILL.md"))]
    agents = [{"name": f"chef:{role}", "source": {"type": "plugin", "path": os.path.join(doctor.PLUGIN, "grok", "agents", f"{role}.md")}}
              for role in doctor.GROK_NATIVE_ROLES]
    return {"projectTrusted": trusted, "skills": skills, "agents": agents,
            "plugins": [{"name": "chef", "path": doctor.PLUGIN, "enabled": True, "provides": {"agents": 1}}],
            "hooks": [{"event": "(plugin)", "hookType": "file", "target": "/example/plugins/chef/hooks/hooks.json"}]}


def test_grok_doctor_reports_discovery_separately_from_trust(monkeypatch):
    calls = []
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/example/grok")

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 10
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="grok 1.0.70")
        return SimpleNamespace(returncode=0, stdout=json.dumps(grok_inspection()))

    monkeypatch.setattr(doctor.subprocess, "run", run)
    report = doctor.Report()
    doctor.check_grok(report)
    assert calls == [["/example/grok", "--version"], ["/example/grok", "inspect", "--json"]]
    assert levels(report, "grok-cli") == ["OK"]
    assert levels(report, "grok-skills") == ["OK"]
    assert levels(report, "grok-agents") == ["OK"]
    assert levels(report, "grok-native") == ["OK"]
    assert levels(report, "grok-hooks") == ["OK"]
    assert levels(report, "grok-trust") == ["WARN"]
    assert any("로드·실행 검증이 아님" in row["text"] for row in report.rows)
    assert any("하위 에이전트 상태는 저장된 metadata·lifecycle 기준" in row["text"] for row in report.rows)


def test_grok_doctor_missing_binary_fails_without_running_it(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "")
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not launch missing CLI"))
    report = doctor.Report()
    doctor.check_grok(report)
    assert levels(report, "grok-cli") == ["FAIL"]


@pytest.mark.parametrize("target", ["/example/plugins/chef/hooks/hooks.json", r"C:\\example\\plugins\\chef\\hooks/hooks.json"])
def test_grok_hook_registration_accepts_native_and_compat_paths(target):
    assert doctor.grok_han_hook({"target": target})
    assert not doctor.grok_han_hook({"target": "/example/plugins/other/hooks/hooks.json"})


@pytest.mark.parametrize("output,code", [("not JSON", 0), ("[]", 0), ("{}", 1)])
def test_grok_doctor_inspection_failures_are_explicit(monkeypatch, output, code):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/example/grok")

    def run(argv, **kwargs):
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="grok 1.0.70")
        return SimpleNamespace(returncode=code, stdout=output)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    report = doctor.Report()
    doctor.check_grok(report)
    assert levels(report, "grok-registration") == ["WARN"]
    assert not levels(report, "grok-trust")


def test_grok_doctor_missing_registration_is_a_warning(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/example/grok")

    def run(argv, **kwargs):
        if argv[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout="grok 1.0.70")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"projectTrusted": True, "skills": [], "hooks": []}))

    monkeypatch.setattr(doctor.subprocess, "run", run)
    report = doctor.Report()
    doctor.check_grok(report)
    assert levels(report, "grok-skills") == ["WARN"]
    assert levels(report, "grok-agents") == ["WARN"]
    assert levels(report, "grok-native") == ["WARN"]
    assert levels(report, "grok-hooks") == ["WARN"]
    assert levels(report, "grok-trust") == ["OK"]


@pytest.mark.parametrize("change,detail", [
    ("missing", "없음: chef:reviewer-small"),
    ("wrong-name", "예상 밖 역할: chef:other"),
    ("compat-source", "원본 경로 불일치·중복: chef:reviewer-small"),
    ("missing-source", "원본 경로 불일치·중복: chef:reviewer-small"),
    ("duplicate", "원본 경로 불일치·중복: chef:reviewer-small"),
])
def test_grok_doctor_reports_missing_or_wrong_native_roles(change, detail):
    inspection = grok_inspection()
    reviewer = inspection["agents"][-1]
    if change == "missing":
        inspection["agents"].pop()
    elif change == "wrong-name":
        reviewer["name"] = "chef:other"
    elif change == "compat-source":
        reviewer["source"]["path"] = os.path.join(doctor.PLUGIN, "agents", "reviewer-small.md")
    elif change == "missing-source":
        reviewer.pop("source")
    else:
        inspection["agents"].append(dict(reviewer))
    report = doctor.Report()
    doctor.check_grok_agents(report, inspection)
    assert levels(report, "grok-agents") == ["WARN"]
    row = next(row for row in report.rows if row["area"] == "grok-agents")
    assert detail in row["text"]
    assert "5/6개" in row["text"]


@pytest.mark.parametrize("change", ["wrong-path", "disabled", "missing", "duplicate"])
def test_grok_doctor_rejects_inactive_or_wrong_selected_plugin(change):
    inspection = grok_inspection()
    plugin = inspection["plugins"][0]
    if change == "wrong-path":
        plugin["path"] = os.path.join(doctor.PLUGIN, "other")
    elif change == "disabled":
        plugin["enabled"] = False
    elif change == "missing":
        inspection.pop("plugins")
    else:
        inspection["plugins"].append(dict(plugin))
    report = doctor.Report()
    doctor.check_grok_agents(report, inspection)
    assert levels(report, "grok-native") == ["WARN"]


def test_grok_doctor_does_not_use_registration_directory_count_as_role_count():
    inspection = grok_inspection()
    inspection["agents"].append({"name": "other:researcher", "source": {"type": "plugin", "path": "/other/researcher.md"}})
    report = doctor.Report()
    doctor.check_grok_agents(report, inspection)
    assert levels(report, "grok-agents") == ["OK"]
    assert levels(report, "grok-native") == ["OK"]
    assert any("파견·모델·effort 실행 검증이 아님" in row["text"] for row in report.rows)


def shipped_role(name):
    return os.path.join(doctor.PLUGIN, "codex", "agents", name)


def shipped_text(name):
    with open(shipped_role(name), encoding="utf-8") as f:
        return f.read()


def codex_agent_rows(report):
    return [(row["level"], row["text"]) for row in report.rows if row["area"] == "codex-agents"]


def shipped_files():
    folder = os.path.join(doctor.PLUGIN, "codex", "agents")
    return sorted(name for name in os.listdir(folder) if name.endswith(".toml"))


def register_codex_role(tmp_path, name, text):
    folder = tmp_path / "codex-home" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(text.encode("utf-8"))
    return folder / name


def test_doctor_codex_agents_none_registered_is_info_only(tmp_path, monkeypatch):
    adapters, _ = make_registry(tmp_path)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(adapters))
    report = doctor.run("codex")
    rows = codex_agent_rows(report)
    assert [level for level, _ in rows] == ["OK"] and "등록 없음" in rows[0][1]
    assert not report.failed


def test_doctor_codex_agents_full_matching_copy_is_ok_even_with_crlf(tmp_path):
    for name in shipped_files():
        register_codex_role(tmp_path, name, shipped_text(name).replace("\n", "\r\n"))
    report = doctor.Report()
    doctor.check_codex_agents(report)
    ((level, detail),) = codex_agent_rows(report)
    assert level == "OK" and "낡은" not in detail and "빠진" not in detail
    assert os.path.join(doctor.PLUGIN, "codex", "agents") in detail


def test_doctor_codex_agents_partial_registration_names_missing_roles(tmp_path):
    register_codex_role(tmp_path, "chef-researcher.toml", shipped_text("chef-researcher.toml"))
    report = doctor.Report()
    doctor.check_codex_agents(report)
    ((level, detail),) = codex_agent_rows(report)
    assert level == "WARN"
    assert "빠진 역할" in detail and "chef_verifier" in detail and "chef_researcher" not in detail.split("빠진 역할")[1]


def test_doctor_codex_agents_compares_by_role_name_not_file_name(tmp_path):
    for name in shipped_files():
        if name != "chef-verifier.toml":
            register_codex_role(tmp_path, name, shipped_text(name))
    register_codex_role(tmp_path, "my-verifier.toml", shipped_text("chef-verifier.toml").replace("\"chef_verifier\"", "'chef_verifier'") + "\n# 오래된 사본\n")
    report = doctor.Report()
    doctor.check_codex_agents(report)
    ((level, detail),) = codex_agent_rows(report)
    assert level == "WARN" and "my-verifier.toml" in detail and "낡은 등록" in detail
    assert "빠진 역할" not in detail


def test_doctor_codex_agents_role_name_ignores_nested_tables_and_reads_literal_strings():
    assert doctor.role_name_by_pattern("name = 'chef_a'\n") == "chef_a"
    assert doctor.role_name_by_pattern("[x]\nname = \"chef_b\"\n") == ""
    assert doctor.role_name("name = 'chef_c'\n") == "chef_c"
    assert doctor.role_name("broken = [\nname = \"chef_d\"\n") == "chef_d"


def test_doctor_codex_agents_os_error_is_reported_and_doctor_finishes(tmp_path, monkeypatch):
    registered = os.path.join(str(tmp_path / "codex-home"), "agents")
    (tmp_path / "codex-home" / "agents").mkdir(parents=True)
    real_listdir = os.listdir

    def listdir(path="."):
        if os.fspath(path) == registered:
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(doctor.os, "listdir", listdir)
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "none"))
    report = doctor.run("codex")
    ((level, detail),) = codex_agent_rows(report)
    assert level == "WARN" and "PermissionError" in detail
    assert levels(report, "registry") == ["FAIL"]


def test_doctor_codex_agents_reports_stale_and_unknown_chef_roles(tmp_path):
    stale = register_codex_role(tmp_path, "chef-verifier.toml", shipped_text("chef-verifier.toml") + "\n# 오래된 사본\n")
    register_codex_role(tmp_path, "chef-retired.toml", 'name = "chef_retired"\n')
    register_codex_role(tmp_path, "renamed.toml", 'name = "chef_renamed"\n')
    other = register_codex_role(tmp_path, "my-reviewer.toml", 'name = "my_reviewer"\n')
    report = doctor.Report()
    doctor.check_codex_agents(report)
    ((level, detail),) = codex_agent_rows(report)
    assert level == "WARN"
    assert "낡은 등록" in detail and "chef-verifier.toml" in detail
    assert "chef-retired.toml" in detail and "renamed.toml" in detail
    assert "my-reviewer" not in detail
    assert stale.exists() and other.exists()
    assert not report.failed
