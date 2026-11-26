"""Invented external documents and role definitions keep explicit authority."""

import hashlib
import json
import os

import pytest

from chef.runtimes.hermes.contracts import ReadSources, preflight, role_contract


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_source_hash_and_paging_are_checked_on_every_read(tmp_path):
    document = tmp_path.resolve() / "guide.md"
    document.write_text("approved guidance")
    sources = ReadSources({str(document): fingerprint(document)})
    assert sources.read(str(document), 9)["text"] == "guidance"
    with pytest.raises(ValueError, match="approved"):
        sources.read(str(tmp_path / "unlisted.md"))
    document.write_text("changed guidance")
    with pytest.raises(ValueError, match="changed"):
        sources.read(str(document))


def test_role_policy_is_validated_before_a_model_can_be_launched(tmp_path):
    role = tmp_path.resolve() / "role.toml"
    role.write_text('model = "chosen-model"\nmodel_reasoning_effort = "high"\ndeveloper_instructions = "Review sample files."\n')
    settings = {"roleContracts": {"researcher": {"path": role.name, "sha256": fingerprint(role), "modelPolicy": "definition"}}}
    preflight(settings, tmp_path, ["researcher"])
    routing, body = role_contract(settings, tmp_path, "researcher")
    assert routing == {"model": "chosen-model"} and body == "Review sample files."
    with pytest.raises(ValueError, match="conflicts"):
        role_contract(settings, tmp_path, "researcher", {"model": "another"})
    role.write_text('developer_instructions = "unapproved"\n')
    with pytest.raises(ValueError, match="changed"):
        role_contract(settings, tmp_path, "researcher")


def test_inherited_hermes_model_does_not_import_codex_effort(tmp_path):
    role = tmp_path.resolve() / "role.toml"
    role.write_text('model_reasoning_effort = "high"\ndeveloper_instructions = "Inspect sample files."\n')
    settings = {"roleContracts": {"researcher": {"path": role.name, "sha256": fingerprint(role), "modelPolicy": "inherit"}}}
    assert role_contract(settings, tmp_path, "researcher") == ({}, "Inspect sample files.")


@pytest.mark.skipif(os.name != "posix", reason="Hermes descriptor reads require POSIX")
def test_approved_source_cannot_be_replaced_by_a_link(tmp_path):
    document = tmp_path.resolve() / "guide.md"
    other = tmp_path.resolve() / "other.md"
    document.write_text("approved")
    other.write_text("approved")
    sources = ReadSources({str(document): fingerprint(document)})
    document.unlink()
    document.symlink_to(other)
    with pytest.raises(ValueError, match="links"):
        sources.read(str(document))
