"""Global preferences are optional and never silently reset when malformed."""

from pathlib import Path

import pytest

from chef.infrastructure import user_config


def test_configuration_uses_os_user_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert user_config.path() == tmp_path / ".chef" / "config.json"
    assert user_config.read() == {}
    assert not (tmp_path / ".chef").exists()


@pytest.mark.parametrize("payload", [b'{"workers":{"mode":"native"},"future":{"enabled":true}}',
                                     b'\xef\xbb\xbf{"workers":{"mode":"native"},"future":{"enabled":true}}'])
def test_read_supports_utf8_bom_and_preserves_other_preferences(monkeypatch, tmp_path, payload):
    source = tmp_path / "config.json"
    source.write_bytes(payload)
    monkeypatch.setattr(user_config, "path", lambda: source)
    assert user_config.read() == {"workers": {"mode": "native"}, "future": {"enabled": True}}
    assert source.read_bytes() == payload


def test_oversize_configuration_is_rejected(monkeypatch, tmp_path):
    source = tmp_path / "config.json"
    source.write_bytes(b" " * (user_config.MAX_BYTES + 1))
    monkeypatch.setattr(user_config, "path", lambda: source)
    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        user_config.read()
