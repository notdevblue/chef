"""User routing rules bound automatic choice across all service combinations."""

import itertools
import json

import pytest

from chef.workers import policy


@pytest.fixture
def preferences(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(policy.user_config, "path", lambda: path)
    monkeypatch.delenv("CHEF_WORKERS_MODE", raising=False)

    def write(workers):
        path.write_text(json.dumps({"workers": workers}), encoding="utf-8")

    return write


def resolve(request=None, settings=None, *, owner="codex", role="dev-medium"):
    return policy.resolve(request or {}, settings or {}, owner_runtime=owner, role=role)


@pytest.mark.parametrize("owner,role", list(itertools.product(policy.RUNTIMES, policy.ROLES)))
def test_default_policy_keeps_all_services_available(preferences, owner, role):
    result = resolve(owner=owner, role=role)
    assert result == {"mode": "auto", "runtimes": list(policy.RUNTIMES),
                      "roleRouting": {}, "allowedRuntimes": list(policy.RUNTIMES)}


@pytest.mark.parametrize("subset", [["claude"], ["codex"], ["grok"],
    ["claude", "codex"], ["claude", "grok"], ["codex", "grok"]])
def test_global_runtime_subsets(preferences, subset):
    preferences({"runtimes": subset})
    assert resolve()["allowedRuntimes"] == subset


@pytest.mark.parametrize("runtime,role", list(itertools.product(policy.RUNTIMES, policy.ROLES)))
def test_each_role_can_pin_any_service(preferences, runtime, role):
    preferences({"roleRouting": {role: {"runtime": runtime}}})
    assert resolve(role=role)["allowedRuntimes"] == [runtime]


def test_fields_resolve_independently_and_role_overrides_preserve_other_roles(preferences, monkeypatch):
    preferences({"mode": "claude", "runtimes": ["claude"], "roleRouting": {
        "code-reviewer": {"runtime": "claude"}, "reviewer-small": {"runtime": "claude"}}})
    settings = {"mode": "codex", "runtimes": ["claude", "codex"],
                "roleRouting": {"code-reviewer": {"runtime": "codex"}}}
    monkeypatch.setenv("CHEF_WORKERS_MODE", "grok")
    request = {"mode": "auto", "runtimes": ["claude", "codex", "grok"],
               "roleRouting": {"reviewer-small": {"runtime": "auto"}}}
    result = resolve(request, settings, role="code-reviewer")
    assert result["mode"] == "auto" and result["runtimes"] == request["runtimes"]
    assert result["roleRouting"] == {"code-reviewer": {"runtime": "codex"},
                                     "reviewer-small": {"runtime": "auto"}}
    assert result["allowedRuntimes"] == ["codex"]
    assert resolve(request, settings, role="reviewer-small")["allowedRuntimes"] == request["runtimes"]


def test_project_runtime_list_replaces_global_list(preferences):
    preferences({"runtimes": ["claude"]})
    assert resolve(settings={"runtimes": ["codex"]})["allowedRuntimes"] == ["codex"]


@pytest.mark.parametrize("runtimes", [[], None, "claude", {}, ["claude", "claude"],
                                      ["unknown"], [False], [["claude"]]])
def test_invalid_runtime_lists_are_rejected(preferences, runtimes):
    preferences({"runtimes": runtimes})
    with pytest.raises(ValueError, match="workers.runtimes"):
        resolve()


@pytest.mark.parametrize("rules", [None, [], {"review": {"runtime": "claude"}},
    {"code-reviewer": None}, {"code-reviewer": {}}, {"code-reviewer": "claude"},
    {"code-reviewer": {"runtime": "native"}}, {"code-reviewer": {"runtime": []}},
    {"code-reviewer": {"runtime": "unknown"}}, {"code-reviewer": {"runtime": "claude", "extra": True}}])
def test_invalid_role_rules_are_rejected(preferences, rules):
    preferences({"roleRouting": rules})
    with pytest.raises(ValueError, match="roleRouting"):
        resolve()


def test_excluded_pin_rejects_whole_configuration_before_any_role_runs(preferences):
    preferences({"runtimes": ["codex"], "roleRouting": {"code-reviewer": {"runtime": "claude"}}})
    with pytest.raises(ValueError, match="roleRouting.code-reviewer conflicts with workers.runtimes"):
        resolve(role="researcher")


@pytest.mark.parametrize("mode", ["native", "codex"])
def test_mode_cannot_escape_allowed_services(preferences, mode):
    preferences({"runtimes": ["claude"]})
    with pytest.raises(ValueError, match="mode conflicts with workers.runtimes"):
        resolve({"mode": mode})


@pytest.mark.parametrize("mode", ["native", "codex"])
def test_single_service_mode_cannot_override_other_role_pin(preferences, mode):
    preferences({"roleRouting": {"code-reviewer": {"runtime": "claude"}}})
    with pytest.raises(ValueError, match="mode conflicts with workers.roleRouting"):
        resolve({"mode": mode}, role="dev-small")


def test_auto_role_override_explicitly_releases_inherited_pin(preferences):
    preferences({"roleRouting": {"code-reviewer": {"runtime": "claude"}}})
    result = resolve({"mode": "native", "roleRouting": {"code-reviewer": {"runtime": "auto"}}})
    assert result["allowedRuntimes"] == ["codex"]
