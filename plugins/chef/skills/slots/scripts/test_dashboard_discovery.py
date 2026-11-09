import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dashboard
import dashboard_discovery
import mission_guard
import slots
from test_dashboard import directory_alias


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def adapter(state="missions", **values):
    return {"schema": 1, "project": "Example", "paths": {"stateDir": state}, **values}


def mission(root, name="example-mission", state="missions", active=False):
    folder = root / state / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mission.md").write_text(f"# {name}\n\n상태: 진행\n현재 단계: build\n\n## 목표\n\nExample goal\n", encoding="utf-8")
    if active:
        write_json(folder.parent / "ACTIVE", {"id": name, "session": ""})
    return folder


@pytest.fixture
def setup(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    registry.mkdir()
    write_json(registry / "projects.json", [])
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(registry))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    root = tmp_path / "current"
    path = root / ".claude/orchestrator/adapter.json"
    write_json(path, adapter())
    return root, registry, path


def config(root, runtime="claude"):
    located = slots.locate(str(root), runtime)
    return dashboard_discovery.Config(located, slots.read_adapter(located.adapter_path, runtime), runtime)


def register(registry, name, roots, data=None):
    write_json(registry / name / "adapter.json", data or adapter())
    path = registry / "projects.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries.append({"adapter": name, "roots": [str(root) for root in roots]})
    write_json(path, entries)


def missions(cfg):
    return [(location, item) for location in dashboard.collect(cfg) for item in location.missions]


def test_dashboard_without_slots_keeps_main_mission_and_cli_requirement(setup):
    root, _, _ = setup
    folder = mission(root)
    cfg = config(root)
    location, item = missions(cfg)[0]
    assert location.key == "main" and item.active is False
    assert dashboard.route(cfg, dashboard.mission_url(item))[0] == 200
    assert dashboard.route(cfg, dashboard.view_url(folder / "mission.md"))[0] == 200
    assert b"example-mission" in dashboard.route(cfg, "/")[2]
    with pytest.raises(slots.UsageError, match="slots"):
        slots.Config(cfg.located, cfg.adapter)


def test_dashboard_enabled_slots_need_no_client_but_slot_cli_does(setup, tmp_path):
    root, _, path = setup
    write_json(path, adapter(slots={"enabled": True, "root": str(tmp_path / "slots"), "names": ["slot1"]}))
    cfg = config(root)
    assert [item.key for item in dashboard.locations(cfg)] == ["main", "slot1"]
    with pytest.raises(slots.UsageError, match="client"):
        slots.Config(cfg.located, cfg.adapter)


def test_dashboard_entrypoint_starts_without_slot_configuration(setup, monkeypatch):
    root, _, _ = setup
    mission(root)
    seen = []

    class Server:
        def __init__(self, cfg, port):
            seen.extend(item.id for _, item in missions(cfg))
            self.server_address = ("127.0.0.1", port)

        def serve_forever(self):
            raise KeyboardInterrupt()

        def server_close(self):
            seen.append("closed")

    monkeypatch.setattr(dashboard, "Server", Server)
    assert dashboard.main(["--project", str(root)]) == slots.OK
    assert seen == ["example-mission", "closed"]


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_registry_exact_and_segment_glob_find_missions_outside_start(setup, tmp_path, runtime):
    root, registry, _ = setup
    first, second = tmp_path / "other" / "one", tmp_path / "other" / "two"
    mission(first, "one")
    mission(second, "two")
    mission(second / "nested", "too-deep")
    register(registry, "external", [first, tmp_path / "other" / "*"])
    found = missions(config(root, runtime))
    assert {item.id for _, item in found} == {"one", "two"}
    assert len({location.key for location, _ in found}) == 2
    assert all(dashboard.route(config(root, runtime), dashboard.mission_url(item))[0] == 200 for _, item in found)


def test_registry_and_new_missions_are_discovered_on_each_request(setup, tmp_path):
    root, registry, _ = setup
    cfg = config(root)
    assert missions(cfg) == []
    external = tmp_path / "added"
    mission(external, "first")
    register(registry, "added", [external])
    assert {item.id for _, item in missions(cfg)} == {"first"}
    mission(external, "later")
    assert {item.id for _, item in missions(cfg)} == {"first", "later"}


def test_duplicate_roots_keep_slot_links_and_colliding_labels_have_stable_keys(setup, tmp_path):
    root, registry, path = setup
    slot_root = tmp_path / "slots"
    settings = {"enabled": True, "root": str(slot_root), "names": ["slot1"]}
    write_json(path, adapter(slots=settings))
    mission(slot_root / "slot1", "original")
    other = tmp_path / "other"
    mission(other, "other")
    register(registry, "same", [root, slot_root / "slot1"], adapter(slots=settings))
    register(registry, "other", [other, other])
    cfg = config(root)
    found = missions(cfg)
    assert len(found) == 2
    original = next(item for _, item in found if item.id == "original")
    additional = next(item for _, item in found if item.id == "other")
    assert original.location_key == "slot1"
    assert additional.location_key.startswith("workspace-")
    stable = additional.location_key
    entries = json.loads((registry / "projects.json").read_text(encoding="utf-8"))
    write_json(registry / "projects.json", list(reversed(entries)))
    assert next(item for _, item in missions(cfg) if item.id == "other").location_key == stable
    assert dashboard.route(cfg, "/mission?slot=slot1&id=original")[0] == 200


def test_each_project_uses_its_own_stages_and_stale_limit(setup, tmp_path, monkeypatch):
    root, registry, path = setup
    write_json(path, adapter(stages=[{"id": "build", "label": "Current stage"}], mission={"staleHours": 3}))
    mission(root, "current", active=True)
    external = tmp_path / "external"
    mission(external, "external", active=True)
    register(registry, "external", [external], adapter(stages=[{"id": "build", "label": "External stage"}], mission={"staleHours": 7}))
    limits = []

    def staleness(state_dir, active, limit):
        limits.append((state_dir, limit))
        return mission_guard.Staleness(True, 1, 100, limit)

    monkeypatch.setattr(mission_guard, "staleness", staleness)
    cfg = config(root)
    found = missions(cfg)
    assert {limit for _, limit in limits} == {3, 7}
    for location, item in found:
        stage = "Current stage" if item.id == "current" else "External stage"
        assert stage in dashboard.slot_card(cfg, location)
        assert stage in dashboard.route(cfg, dashboard.mission_url(item))[2].decode("utf-8")
        assert f"기준 {location.stale_hours:g}시간" in item.stale


@pytest.mark.parametrize("state", ["absolute", "relative"])
def test_explicit_external_state_dir_keeps_document_sharing_boundary(setup, tmp_path, state):
    root, _, path = setup
    external = tmp_path / "mission-store"
    state_path = str(external) if state == "absolute" else "../mission-store"
    write_json(path, adapter(state_path))
    folder = mission(external, state="")
    (folder / "report.md").write_text("Unshared report", encoding="utf-8")
    (tmp_path / "unregistered.md").write_text("Unregistered", encoding="utf-8")
    cfg = config(root)
    _, item = missions(cfg)[0]
    assert dashboard.route(cfg, dashboard.mission_url(item))[0] == 200
    for url in (dashboard.view_url(folder / "mission.md"), dashboard.raw_url(folder / "mission.md")):
        assert dashboard.route(cfg, url)[0] == 200
    assert dashboard.route(cfg, dashboard.view_url(folder / "report.md"))[0] == 404
    assert dashboard.route(cfg, dashboard.raw_url(tmp_path / "unregistered.md"))[0] == 403


def test_external_state_dir_keeps_shared_manifest_hash_checks(setup, tmp_path):
    root, _, path = setup
    external = tmp_path / "mission-store"
    write_json(path, adapter(str(external)))
    folder = mission(external, state="")
    report = folder / "report.md"
    report.write_text("Reviewed example report", encoding="utf-8")
    cfg = config(root)
    state = dashboard.mission_state
    state.write_table(str(folder / state.SHARED_DOCUMENTS_FILE), state.SHARED_DOCUMENTS_HEADER, [{
        "path": "report.md", "sha256": dashboard.digest(report.read_bytes()), "at": "2026-01-01T00:00:00+00:00",
    }])
    for url in (dashboard.view_url(report), dashboard.raw_url(report)):
        assert dashboard.route(cfg, url)[0] == 200
    report.write_text("Changed example report", encoding="utf-8")
    for url in (dashboard.view_url(report), dashboard.raw_url(report)):
        assert dashboard.route(cfg, url)[0] == 404


@pytest.mark.parametrize("broken", ["registry", "adapter", "local"])
def test_broken_registry_sources_are_visible_in_overview(setup, tmp_path, broken):
    root, registry, _ = setup
    external = tmp_path / "external"
    mission(external)
    register(registry, "external", [external])
    path = {"registry": registry / "projects.json", "adapter": registry / "external/adapter.json", "local": registry / "external/local.json"}[broken]
    path.write_text("{broken", encoding="utf-8")
    response = dashboard.route(config(root), "/")
    assert response[0] == 200
    assert path.name in response[2].decode("utf-8")
    assert "example-mission" not in response[2].decode("utf-8")


def test_corrupt_current_local_is_a_startup_error(setup):
    root, _, path = setup
    (path.parent / "local.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(slots.UsageError, match="local.json"):
        config(root)


@pytest.mark.parametrize("glob", [False, True])
def test_unreadable_registered_root_is_visible(setup, tmp_path, monkeypatch, glob):
    root, registry, _ = setup
    external = tmp_path / "unreadable"
    mission(external)
    register(registry, "external", [external / "*" if glob else external])
    original_scandir, original_stat = os.scandir, os.stat

    def scandir(path):
        if Path(path) == external:
            raise PermissionError("example denied")
        return original_scandir(path)

    def stat(path, *args, **kwargs):
        if not glob and Path(path) == external:
            raise PermissionError("example denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "stat", stat)
    source = dashboard.route(config(root), "/")[2].decode("utf-8")
    assert "example denied" in source and "읽기 실패" in source


def test_missing_root_and_unmatched_glob_are_warnings(setup, tmp_path):
    root, registry, _ = setup
    register(registry, "missing", [tmp_path / "missing", tmp_path / "unmatched" / "*"])
    source = dashboard.route(config(root), "/")[2].decode("utf-8")
    assert "등록 경로 없음" in source and "missing" in source and "unmatched" in source


def test_registry_parent_and_unregistered_sibling_are_never_allowed(setup, tmp_path):
    root, registry, _ = setup
    external = tmp_path / "registered" / "workspace"
    mission(external)
    sibling = external.parent / "sibling" / "secret.md"
    sibling.parent.mkdir()
    sibling.write_text("Example private data", encoding="utf-8")
    register(registry, "external", [external])
    for url in (dashboard.view_url(sibling), dashboard.raw_url(sibling)):
        assert dashboard.route(config(root), url)[0] == 403


def test_external_mission_junction_replacement_stays_hidden(setup, tmp_path):
    root, _, path = setup
    external = tmp_path / "mission-store"
    write_json(path, adapter(str(external)))
    folder = mission(external, state="")
    cfg = config(root)
    assert len(missions(cfg)) == 1
    other = tmp_path / "private"
    mission(other, state="")
    (folder / "mission.md").unlink()
    folder.rmdir()
    directory_alias(folder, other / "example-mission")
    assert missions(cfg) == []
    for url in (dashboard.view_url(folder / "mission.md"), dashboard.raw_url(folder / "mission.md")):
        assert dashboard.route(cfg, url)[0] in (403, 404)


def test_runtime_and_local_overlay_are_reused(setup, tmp_path):
    root, registry, _ = setup
    external = tmp_path / "external"
    mission(external, state="local-codex")
    register(registry, "external", [external], adapter(runtimes={"codex": {"paths": {"stateDir": "base-codex"}}}))
    write_json(registry / "external/local.json", {"paths": {"stateDir": "local"}, "runtimes": {"codex": {"paths": {"stateDir": "local-codex"}}}})
    assert len(missions(config(root, "codex"))) == 1
    assert missions(config(root, "claude")) == []


@pytest.mark.parametrize("linked", [False, True])
def test_glob_matches_without_valid_missions_never_become_document_roots(setup, tmp_path, linked):
    root, registry, _ = setup
    groups = tmp_path / "groups"
    healthy = groups / "healthy"
    mission(healthy, "healthy")
    empty = groups / "empty"
    empty.mkdir()
    secret = empty / "private.md"
    secret.write_text("Fictional private document", encoding="utf-8")
    if linked:
        target = tmp_path / "unregistered"
        folder = mission(target)
        (empty / "missions").mkdir()
        directory_alias(empty / "missions" / "linked", folder)
    register(registry, "groups", [groups / "*"])
    cfg = config(root)
    assert {item.id for _, item in missions(cfg)} == {"healthy"}
    assert all(Path(location.root) != empty for location in dashboard.locations(cfg))
    for url in (dashboard.view_url(secret), dashboard.raw_url(secret)):
        assert dashboard.route(cfg, url)[0] == 403
    (healthy / "missions/healthy/mission.md").unlink()
    assert dashboard.route(cfg, dashboard.view_url(healthy / "anything.md"))[0] == 403


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok"])
def test_registered_workspace_selects_its_closest_local_adapter(setup, tmp_path, runtime):
    root, registry, _ = setup
    parent = tmp_path / "other"
    external = parent / "workspace"
    mission(external, "registry-decoy")
    register(registry, "external", [external], adapter(stages=[{"id": "build", "label": "Registry stage"}]))
    write_json(parent / ".claude/orchestrator/adapter.json", adapter("parent-state"))
    local = external / ".claude/orchestrator/adapter.json"
    write_json(local, adapter("local-state", stages=[{"id": "build", "label": "Local stage"}], mission={"staleHours": 11}))
    if runtime == "codex":
        local = external / ".codex/orchestrator/adapter.json"
        write_json(local, adapter("codex-state", stages=[{"id": "build", "label": "Codex stage"}], mission={"staleHours": 13},
                                  runtimes={"codex": {"paths": {"stateDir": "codex-overlay"}}}))
        write_json(local.parent / "local.json", {"runtimes": {"codex": {"paths": {"stateDir": "final-codex"}}}})
    state, label, hours = ("final-codex", "Codex stage", 13) if runtime == "codex" else ("local-state", "Local stage", 11)
    folder = mission(external, "selected-local", state=state)
    cfg = config(root, runtime)
    found = missions(cfg)
    assert {item.id for _, item in found} == {"selected-local"}
    location, item = found[0]
    assert Path(location.state_dir) == folder.parent and location.stale_hours == hours
    assert label in dashboard.slot_card(cfg, location)
    assert dashboard.route(cfg, dashboard.mission_url(item))[0] == 200


def test_broken_codex_registry_resolution_keeps_current_and_existing_slot_missions(setup, tmp_path, monkeypatch):
    root, _, path = setup
    slot_root = tmp_path / "slots"
    write_json(path, adapter(slots={"enabled": True, "root": str(slot_root), "names": ["slot1"]}))
    mission(root, "current")
    mission(slot_root / "slot1", "slot")
    cfg = config(root, "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("[broken", encoding="utf-8")
    monkeypatch.delenv("HAN_ADAPTERS_DIR")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert {item.id for _, item in missions(cfg)} == {"current", "slot"}
    status, _, body = dashboard.route(cfg, "/")
    source = body.decode("utf-8")
    assert status == 200 and "config.toml" in source and "읽기 실패" in source
    assert "current" in source and "slot" in source


@pytest.mark.parametrize("wildcard_child", [False, True])
def test_glob_branch_read_error_keeps_healthy_siblings(setup, tmp_path, monkeypatch, wildcard_child):
    root, registry, _ = setup
    groups = tmp_path / "groups"
    denied = groups / "a-denied"
    healthy = groups / "b-healthy" / "workspace"
    denied.mkdir(parents=True)
    mission(healthy, "healthy-sibling")
    pattern = groups / "*" / ("work*" if wildcard_child else "workspace")
    register(registry, "groups", [pattern])
    original_scandir, original_stat = os.scandir, os.stat

    def scandir(path):
        if wildcard_child and Path(path) == denied:
            raise PermissionError("fictional branch denied")
        return original_scandir(path)

    def stat(path, *args, **kwargs):
        if not wildcard_child and Path(path) == denied / "workspace":
            raise PermissionError("fictional branch denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "stat", stat)
    cfg = config(root)
    assert {item.id for _, item in missions(cfg)} == {"healthy-sibling"}
    source = dashboard.route(cfg, "/")[2].decode("utf-8")
    assert "fictional branch denied" in source and "healthy-sibling" in source


def test_metadata_escape_is_excluded_from_list_and_direct_mission_detail(setup, tmp_path, monkeypatch):
    root, registry, _ = setup
    external = tmp_path / "external"
    healthy = mission(external, "healthy")
    register(registry, "external", [external])
    private = tmp_path / "outside.md"
    private.write_text("# Fictional private title\n\n상태: 진행\n\n## 목표\n\nFictional private goal\n", encoding="utf-8")
    alias = healthy.parent / "alias"
    alias.mkdir()
    metadata = alias / "mission.md"
    try:
        metadata.symlink_to(private)
    except OSError:
        metadata.write_bytes(private.read_bytes())
        realpath = os.path.realpath

        def resolved_metadata(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(metadata)):
                return str(private)
            return realpath(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", resolved_metadata)
    cfg = config(root)
    found = missions(cfg)
    assert {item.id for _, item in found} == {"healthy"}
    location, _ = found[0]
    status, _, body = dashboard.route(cfg, f"/mission?slot={location.key}&id=alias")
    assert status == 404
    assert "Fictional private" not in body.decode("utf-8")
    loaded = dashboard.load_mission(location, "alias")
    assert loaded.fields == {} and loaded.goal == "" and loaded.title == ""
    assert "mission.md 경로" in loaded.errors
    for url in (dashboard.view_url(metadata), dashboard.raw_url(metadata)):
        assert dashboard.route(cfg, url)[0] == 403
