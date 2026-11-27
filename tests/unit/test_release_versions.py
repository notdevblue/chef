"""Installer-specific version declarations must describe one release."""

import json

import pytest

from tools.check_architecture import inspect_versions


@pytest.fixture
def release(tmp_path):
    manifests = {
        ".claude-plugin/marketplace.json": {"plugins": [{"name": "chef", "version": "1.2.3"}]},
        **{"plugins/chef/." + runtime + "-plugin/plugin.json": {"version": "1.2.3"}
           for runtime in ("claude", "codex", "grok")},
    }
    for name, data in manifests.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    hermes = tmp_path / "plugins/chef/hermes/plugin.yaml"
    hermes.parent.mkdir()
    hermes.write_text("name: chef\nversion: 1.2.3\n", encoding="utf-8")
    return tmp_path


def test_all_native_release_versions_match(release):
    assert inspect_versions(release) == []


@pytest.mark.parametrize("manifest", [".claude-plugin/marketplace.json",
                                     "plugins/chef/.claude-plugin/plugin.json",
                                     "plugins/chef/.codex-plugin/plugin.json",
                                     "plugins/chef/.grok-plugin/plugin.json",
                                     "plugins/chef/hermes/plugin.yaml"])
def test_each_stale_installer_manifest_is_rejected(release, manifest):
    path = release / manifest
    path.write_text(path.read_text(encoding="utf-8").replace("1.2.3", "1.2.2"), encoding="utf-8")
    assert "release versions differ" in inspect_versions(release)[0]


def test_missing_native_version_is_rejected(release):
    (release / "plugins/chef/hermes/plugin.yaml").write_text("name: chef\n", encoding="utf-8")
    assert inspect_versions(release) == ["release versions must be nonempty strings"]
