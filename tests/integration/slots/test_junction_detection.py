from pathlib import Path

from tests.paths import PLUGIN_ROOT

"""Synthetic Windows 3.11 fallback coverage without touching real workspaces."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from chef.infrastructure import paths as slots


@pytest.mark.parametrize("native", ["missing", None, False])
@pytest.mark.parametrize("attributes,expected", [(0x400, True), (0x410, True), (0x10, False),
                                                 (0, False), (None, False), ("bad", False), (-1, False)])
def test_windows_without_native_junction_detection_uses_nofollow_attributes(monkeypatch, native, attributes, expected):
    calls = []
    path = "fictional-junction"
    path_api = SimpleNamespace(islink=lambda value: False)
    if native != "missing":
        path_api.isjunction = native

    def lstat(value):
        calls.append(value)
        return SimpleNamespace(st_file_attributes=attributes)

    monkeypatch.setattr(slots, "os", SimpleNamespace(name="nt", path=path_api, lstat=lstat))
    assert slots.is_link(path) is expected
    assert calls == [path]


@pytest.mark.parametrize("result", ["missing-attributes", FileNotFoundError(), PermissionError(), ValueError()])
def test_missing_or_invalid_reparse_metadata_returns_false(monkeypatch, result):
    def lstat(value):
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace()
    monkeypatch.setattr(slots, "os", SimpleNamespace(name="nt", path=SimpleNamespace(islink=lambda _: False), lstat=lstat))
    assert slots.is_link("fictional-missing-node") is False


@pytest.mark.parametrize("platform,native,symlink,expected", [
    ("nt", True, False, True), ("nt", False, False, False),
    ("nt", False, True, True), ("posix", None, True, True), ("posix", None, False, False)])
def test_native_detection_and_posix_symlinks_preserve_existing_behavior(monkeypatch, platform, native, symlink, expected):
    def unexpected_lstat(value):
        pytest.fail("fallback should not inspect this path")
    path_api = SimpleNamespace(islink=lambda _: symlink)
    if native is not None:
        path_api.isjunction = lambda _: native
    monkeypatch.setattr(slots, "os", SimpleNamespace(name=platform, path=path_api, lstat=unexpected_lstat))
    assert slots.is_link("fictional-node") is expected
