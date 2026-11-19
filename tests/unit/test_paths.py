"""Native filesystem identity and containment, with entirely synthetic paths."""

import os

import pytest

from chef.guards.adapters import root_matches
from chef.infrastructure.paths import inside_real_root, inside_root, normalize_path, same_path


def alias(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")


def test_existing_directory_aliases_have_same_identity(tmp_path):
    directory = tmp_path / "garden"
    directory.mkdir()
    shortcut = tmp_path / "shortcut"
    alias(shortcut, directory)
    assert same_path(directory, shortcut)
    assert same_path(directory / "future" / "leaf", shortcut / "future" / "leaf")
    assert inside_real_root(shortcut, directory / "future" / "leaf")
    assert not inside_real_root(directory, shortcut)
    assert not inside_root(shortcut, directory / "future" / "leaf")


def test_registry_matches_exact_and_glob_paths_through_native_aliases(tmp_path):
    directory = tmp_path / "garden"
    plot = directory / "north"
    plot.mkdir(parents=True)
    shortcut = tmp_path / "shortcut"
    alias(shortcut, directory)
    assert root_matches(str(plot), str(shortcut / "north"))
    assert root_matches(str(plot), str(shortcut / "*"))
    assert not root_matches(str(directory), str(shortcut / "*"))


def test_containment_resolves_aliases_before_authorizing(tmp_path):
    directory = tmp_path / "garden"
    outside = tmp_path / "field"
    directory.mkdir()
    outside.mkdir()
    shortcut = directory / "shortcut"
    alias(shortcut, outside)
    assert not inside_real_root(directory, shortcut / "future")
    assert inside_root(directory, shortcut / "future")
    assert not inside_real_root(directory, outside)


def test_distinct_case_sensitive_directories_do_not_share_identity(tmp_path):
    first = tmp_path / "Garden"
    second = tmp_path / "garden"
    first.mkdir()
    if second.exists():
        pytest.skip("filesystem does not distinguish case")
    second.mkdir()
    assert not same_path(first, second)
    assert not same_path(first / "future", second / "future")
    assert not inside_root(first, second / "future")


def test_real_containment_uses_existing_filesystem_case_behavior(tmp_path):
    directory = tmp_path / "Garden"
    directory.mkdir()
    spelling = tmp_path / "garden"
    if spelling.exists():
        assert inside_real_root(directory, spelling / "future")
        assert not inside_real_root(directory, spelling)
    else:
        assert not inside_real_root(directory, spelling / "future")


@pytest.mark.skipif(os.name == "nt", reason="POSIX path spelling")
def test_posix_registry_patterns_preserve_case_and_literal_backslashes():
    assert normalize_path("/Garden/*/") == "/Garden/*"
    assert normalize_path("/garden/back\\slash") == "/garden/back\\slash"


def test_missing_path_is_not_current_directory(tmp_path):
    assert not same_path("", tmp_path)
