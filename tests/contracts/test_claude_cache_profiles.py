"""Cache profiles preserve their original role contract instead of drifting copies."""

from pathlib import Path

from tools.generate_claude_profiles import ROOT, ROLES, generate, render


def test_shipped_profiles_match_canonical_roles():
    assert generate() == []
    agents = ROOT / "plugins/chef/agents"
    for role in ROLES:
        source = (agents / f"{role}.md").read_text(encoding="utf-8")
        profile = (agents / f"{role}-long.md").read_text(encoding="utf-8")
        original_header, original_body = source.split("---", 2)[1:]
        header, body = profile.split("---", 2)[1:]
        assert body == original_body
        assert "experimental:\n  cacheTtl: 1h\n" in header
        for line in original_header.splitlines():
            if line and not line.startswith(("name:", "description:")):
                assert line in header


def test_generator_detects_drift_without_writing(tmp_path):
    agents = tmp_path / "plugins/chef/agents"
    agents.mkdir(parents=True)
    for role in ROLES:
        source = f"---\nname: {role}\ndescription: Fictional role\nmodel: sonnet\neffort: low\n---\nRead the fictional rules.\n"
        (agents / f"{role}.md").write_text(source, encoding="utf-8")
    assert len(generate(tmp_path)) == 3
    assert not list(agents.glob("*-long.md"))
    generate(tmp_path, write=True)
    assert generate(tmp_path) == []
    (agents / "dev-small.md").write_text((agents / "dev-small.md").read_text() + "Updated rules.\n")
    assert generate(tmp_path) == [str(agents / "dev-small-long.md")]
