"""File replacement and extracted-dependency identity regressions."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from chef.infrastructure import storage
from chef.infrastructure.fingerprints import implementation_digest

from tests.paths import PLUGIN_ROOT


@pytest.mark.parametrize("platform,mode", [("nt", 0o777), ("posix", 0o700)])
def test_private_directory_uses_platform_permissions(tmp_path, monkeypatch, platform, mode):
    calls = []
    original = Path.mkdir

    def mkdir(path, mode=0o777, **options):
        calls.append((path, mode, options))
        return original(path, mode=mode, **options)

    monkeypatch.setattr(storage, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(Path, "mkdir", mkdir)
    target = tmp_path / "state"
    storage.make_private_directory(target)
    assert target.is_dir()
    assert calls == [(target, mode, {"parents": True, "exist_ok": True})]


def test_failed_replace_preserves_previous_bytes_and_cleans_temporary(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_bytes(b"previous\n")

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic replace"):
        storage.atomic_write_json(target, {"value": "changed"})
    assert target.read_bytes() == b"previous\n"
    assert list(tmp_path.iterdir()) == [target]


def test_interrupted_stream_creation_closes_descriptor_and_cleans_temporary(tmp_path, monkeypatch):
    descriptors = []

    def interrupted(descriptor, *args):
        descriptors.append(descriptor)
        raise KeyboardInterrupt()

    monkeypatch.setattr(storage.os, "fdopen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        storage.atomic_write_text(tmp_path / "state", "value")
    assert list(tmp_path.iterdir()) == []
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_utf8_bytes_and_durability_are_explicit(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(storage.os, "fsync", lambda descriptor: synced.append(descriptor))
    path = tmp_path / "nested/state.json"
    storage.atomic_write_json(path, {"value": "가상 데이터"}, create_parents=True, durable=True)
    assert path.read_bytes() == '{\n  "value": "가상 데이터"\n}\n'.encode("utf-8")
    assert len(synced) == (1 if os.name == "nt" else 2)


def test_dependency_changes_invalidate_identity_independently_of_install_path(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    for root in (first, second):
        (root / "infrastructure").mkdir(parents=True)
        (root / "infrastructure/locking.py").write_bytes(b"value = 1\n")
    before = implementation_digest(first)
    assert before == implementation_digest(second)
    (second / "infrastructure/locking.py").write_bytes(b"value = 2\n")
    assert before != implementation_digest(second)
    (first / "notes.md").write_text("development notes", encoding="utf-8")
    assert before == implementation_digest(first)


def test_native_newline_option_preserves_existing_text_mode_bytes(tmp_path):
    path = tmp_path / "native.json"
    storage.atomic_write_json(path, {"value": 1}, newline=None)
    expected = '{\n  "value": 1\n}\n'.replace("\n", os.linesep).encode("utf-8")
    assert path.read_bytes() == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask contract")
def test_text_writer_preserves_caller_umask_in_a_separate_process(tmp_path):
    code = (
        "import os,stat,sys;sys.path.insert(0,sys.argv[1]);"
        "from chef.infrastructure.storage import atomic_write_text;"
        "os.umask(0o137);atomic_write_text(sys.argv[2],'value',creation_mode=0o666);"
        "print(oct(stat.S_IMODE(os.stat(sys.argv[2]).st_mode)))"
    )
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(PLUGIN_ROOT), str(tmp_path / "state")],
                            capture_output=True, check=True, timeout=15)
    assert result.stdout.strip() == b"0o640"
