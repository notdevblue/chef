"""Portable preparation behavior in entirely fictional project directories."""

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.guards import adapters
from chef.infrastructure.errors import UsageError
from chef.slots import commands
from chef.slots.configuration import Config
from chef.slots.preparation import PreparationError, copy_tree, link_directory, preflight
from chef.slots import preparation
from tests.paths import REPO_ROOT


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok", "hermes"])
@pytest.mark.parametrize("system", ["windows", "macos", "linux"])
def test_platform_and_runtime_specificity_never_beats_local_values(tmp_path, monkeypatch, runtime, system):
    monkeypatch.setattr(adapters, "platform_name", lambda: system)
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps({"schema": 1, "vars": {"base": "kept", "value": "base"},
        "platforms": {system: {"vars": {"value": "os", "os": "kept"}}},
        "runtimes": {runtime: {"vars": {"value": "runtime"}, "platforms": {system: {"vars": {"value": "runtime-os"}}}}}}))
    assert adapters.load_adapter(str(path), runtime=runtime)["vars"]["value"] == "runtime-os"
    (tmp_path / "local.json").write_text(json.dumps({"vars": {"value": "local"}}))
    assert adapters.load_adapter(str(path), runtime=runtime)["vars"]["value"] == "local"
    (tmp_path / "local.json").write_text(json.dumps({"vars": {"value": "local"},
        "platforms": {system: {"vars": {"value": "local-os"}}},
        "runtimes": {runtime: {"vars": {"value": "local-runtime"}, "platforms": {system: {"vars": {"value": "local-runtime-os"}}}}}}))
    result = adapters.load_adapter(str(path), runtime=runtime)
    assert result["vars"] == {"base": "kept", "os": "kept", "value": "local-runtime-os"}


@pytest.mark.parametrize("value", [[], {"unknown": {}}, {"linux": []}])
def test_invalid_platform_config_is_not_silently_ignored(tmp_path, value):
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps({"schema": 1, "platforms": value}))
    with pytest.raises(adapters.GuardOff):
        adapters.load_adapter(str(path))


def test_copy_checks_all_conflicts_before_writing_and_preserves_extra_files(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir(); destination.mkdir()
    (source / "new").write_text("new")
    (source / "existing").write_text("source")
    (destination / "existing").write_text("user changes")
    (destination / "extra").write_text("retained")
    with pytest.raises(PreparationError, match="conflicts"):
        copy_tree(source, destination)
    assert not (destination / "new").exists()
    assert (destination / "existing").read_text() == "user changes"
    assert (destination / "extra").read_text() == "retained"


def test_copy_is_idempotent_and_dry_run_has_no_filesystem_effect(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir(); (source / "file").write_text("fictional")
    assert copy_tree(source, destination, dry_run=True)["status"] == "planned"
    assert not destination.exists()
    copy_tree(source, destination)
    before = (destination / "file").stat().st_mtime_ns
    copy_tree(source, destination)
    assert (destination / "file").stat().st_mtime_ns == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX special filesystem node")
def test_copy_rejects_existing_fifo_without_opening_it(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir(); destination.mkdir()
    (source / "file").write_text("fictional source")
    os.mkfifo(destination / "file")
    with pytest.raises(PreparationError, match="type conflicts"):
        copy_tree(source, destination)
    assert not (destination / "file").is_file()


def test_file_appearing_after_copy_preflight_is_not_silently_accepted(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir(); (source / "file").write_text("source contents")
    check = preparation.safe_destination
    calls = []
    def race(path):
        calls.append(path)
        if len(calls) == 3:
            destination.mkdir()
            (destination / "file").write_text("concurrent user contents")
        check(path)
    monkeypatch.setattr(preparation, "safe_destination", race)
    with pytest.raises(PreparationError, match="changed during copy"):
        copy_tree(source, destination)
    assert (destination / "file").read_text() == "concurrent user contents"


def make_link(source, destination):
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(source), str(destination))
        else:
            destination.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory links unavailable")


def test_parent_directory_alias_is_valid_but_link_inside_copy_tree_is_not(tmp_path):
    source = tmp_path / "source"; source.mkdir(); (source / "file").write_text("fixture")
    alias = tmp_path / "alias"; make_link(tmp_path, alias)
    copy_tree(source, alias / "destination")
    assert (tmp_path / "destination/file").read_text() == "fixture"
    child = source / "nested"; make_link(tmp_path / "destination", child)
    with pytest.raises(PreparationError, match="link"):
        copy_tree(source, tmp_path / "another")
    assert not (tmp_path / "another").exists()


def test_link_handles_spaces_and_shell_characters_without_overwrite(tmp_path):
    source = tmp_path / "source & data"; source.mkdir()
    destination = tmp_path / "linked & data"
    assert link_directory(source, destination)["status"] == "linked"
    assert link_directory(source, destination)["status"] == "unchanged"
    other = tmp_path / "other"; other.mkdir()
    with pytest.raises(PreparationError):
        link_directory(other, destination)
    assert os.path.samefile(source, destination)
    regular = tmp_path / "existing"; regular.mkdir(); (regular / "keep").write_text("keep")
    with pytest.raises(PreparationError):
        link_directory(source, regular)
    assert (regular / "keep").read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="file symlink permissions are optional on Windows")
@pytest.mark.parametrize("missing", [False, True])
def test_existing_file_or_dangling_link_is_not_a_valid_directory_link(tmp_path, missing):
    source = tmp_path / "source"
    if not missing:
        source.write_text("regular file")
    destination = tmp_path / "alias"
    destination.symlink_to(source)
    with pytest.raises(PreparationError, match="existing directory"):
        link_directory(source, destination)
    assert destination.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="dangling symlink permissions are optional on Windows")
def test_explicit_missing_source_creation_has_read_only_preview(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "alias"; destination.symlink_to(source, target_is_directory=True)
    assert link_directory(source, destination, create_source=True, dry_run=True)["status"] == "planned"
    assert not source.exists()
    assert link_directory(source, destination, create_source=True)["status"] == "unchanged"
    assert source.is_dir() and destination.is_symlink()


@pytest.mark.parametrize("mismatch", ["os", "architecture", "toolVersion", "backend", "inputs", "missing"])
def test_cache_mismatch_or_unknown_identity_skips_copy_without_blocking_import(tmp_path, mismatch):
    source = tmp_path / "cache"; source.mkdir(); (source / "entry").write_text("old cache")
    original = {"os": platform.system().lower(), "architecture": platform.machine().lower(), "toolVersion": "fictional-1", "backend": "fixture", "inputs": "digest-1"}
    actual = dict(original); actual[mismatch] = "different"
    fingerprint = tmp_path / "source.json"; fingerprint.write_text(json.dumps(actual))
    expected = tmp_path / "expected.json"; expected.write_text(json.dumps(original))
    if mismatch == "missing":
        fingerprint.unlink()
    result = copy_tree(source, tmp_path / "target", source_fingerprint=fingerprint, expected_fingerprint=expected)
    assert result["status"] == "skipped" and "import" in result["reason"]
    assert not (tmp_path / "target").exists()


def test_matching_cache_identity_can_be_copied(tmp_path):
    source = tmp_path / "cache"; source.mkdir(); (source / "entry").write_text("cache")
    fingerprint = tmp_path / "fingerprint.json"
    fingerprint.write_text(json.dumps({"os": platform.system().lower(), "architecture": platform.machine().lower(), "toolVersion": "fixture-1", "backend": "fixture", "inputs": "hash-1"}))
    assert copy_tree(source, tmp_path / "target", source_fingerprint=fingerprint, expected_fingerprint=fingerprint)["status"] == "copied"


@pytest.mark.skipif(os.name == "nt", reason="POSIX special node")
def test_special_cache_metadata_is_skipped_without_opening_it(tmp_path):
    source = tmp_path / "cache"; source.mkdir()
    metadata = tmp_path / "fingerprint"
    os.mkfifo(metadata)
    assert copy_tree(source, tmp_path / "target", source_fingerprint=metadata, expected_fingerprint=metadata)["status"] == "skipped"


def test_preflight_requires_tools_and_scripts(tmp_path):
    with pytest.raises(PreparationError):
        preflight(["fictional-executable-that-does-not-exist"])
    with pytest.raises(PreparationError):
        preflight([sys.executable], [str(tmp_path / "absent.py")])


def test_relative_requirements_use_project_cwd_instead_of_process_cwd(tmp_path, monkeypatch):
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    (project / "tools").mkdir(parents=True); elsewhere.mkdir()
    (project / "tools/build.py").write_text("fixture")
    executable = project / "tools" / ("builder.exe" if os.name == "nt" else "builder")
    executable.write_text("fixture"); executable.chmod(0o700)
    monkeypatch.chdir(elsewhere)
    preflight(["tools/" + executable.name], ["tools/build.py"], cwd=project)
    with pytest.raises(PreparationError):
        preflight([sys.executable], ["tools/build.py"])


@pytest.mark.parametrize("runtime", ["claude", "codex", "grok", "hermes"])
def test_public_example_prepares_real_independent_slot_for_each_runtime(tmp_path, monkeypatch, runtime):
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty-registry"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    example = REPO_ROOT / "adapters/example"
    project = tmp_path / "comet"
    shutil.copytree(example / "project", project)
    registration = project / ".hermes/orchestrator" if runtime == "hermes" else project / ".claude/orchestrator"
    registration.mkdir(parents=True)
    shutil.copyfile(example / "adapter.json", registration / "adapter.json")
    (registration / "local.json").write_text(json.dumps({"slots": {"root": str(tmp_path / "slots"), "client": "comet-{name}"}, "vars": {"main": str(project)}}))
    # Calling --project from an unrelated directory must still resolve the
    # adapter's relative prerequisite against that project.
    local = json.loads((registration / "local.json").read_text())
    local["slots"]["requirements"] = {"executables": ["{python}"], "files": ["tools/project.py"]}
    (registration / "local.json").write_text(json.dumps(local))
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir(); monkeypatch.chdir(elsewhere)
    assert commands.main(["--project", str(project), "--runtime", runtime, "create", "slot1"]) == 0
    slot = tmp_path / "slots/slot1"
    assert (slot / "generated/ready.txt").read_text() == "Comet ready\n"
    assert (slot / "src/arithmetic.py").read_bytes() == (project / "src/arithmetic.py").read_bytes()
    config = Config(SimpleNamespace(project=str(project)), adapters.load_adapter(str(registration / "adapter.json"), runtime=runtime), runtime)
    assert bool(config.before_run) == (runtime == "claude")
    assert config.values("slot1")["python"] == sys.executable


def test_missing_requirements_prevent_slot_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("HAN_ADAPTERS_DIR", str(tmp_path / "empty-registry"))
    project = tmp_path / "project"; folder = project / ".hermes/orchestrator"; folder.mkdir(parents=True)
    adapter = {"schema": 1, "paths": {"stateDir": "state"}, "slots": {"enabled": True, "root": str(tmp_path / "slots"), "client": "test-{name}", "names": ["one"], "requirements": {"executables": ["unavailable-fictional-build-tool"]}}}
    (folder / "adapter.json").write_text(json.dumps(adapter))
    assert commands.main(["--project", str(project), "--runtime", "hermes", "create", "one"]) == commands.USAGE
    assert not (tmp_path / "slots").exists()
