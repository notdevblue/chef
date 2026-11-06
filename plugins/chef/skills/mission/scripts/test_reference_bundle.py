import json
import pytest

import reference_bundle


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "guide.md").write_text("Original guidance\n", encoding="utf-8")
    return root


def test_snapshot_keeps_only_pointers_and_checks_changed_missing(project):
    bundle = reference_bundle.snapshot(project, ["guide.md"], "state/reference.json", "r7", "Rules")
    assert set(bundle) == {"schema", "sources"}
    assert bundle["sources"][0]["path"] == "guide.md"
    assert bundle["sources"][0]["revision"] == "r7"
    assert "Original guidance" not in (project / "state/reference.json").read_text()
    assert reference_bundle.check(project, "state/reference.json")["status"] == "pass"
    (project / "guide.md").write_text("Updated guidance\n")
    result = reference_bundle.check(project, "state/reference.json")
    assert result["changed"] == ["guide.md"] and result["missing"] == []
    with pytest.raises(reference_bundle.ReferenceError, match="snapshot"):
        reference_bundle.require_current(project, "state/reference.json")
    (project / "guide.md").unlink()
    result = reference_bundle.check(project, "state/reference.json")
    assert result["missing"] == ["guide.md"] and result["changed"] == []


def test_snapshot_explicit_external_source_and_confined_output(project, tmp_path):
    source = tmp_path / "external.md"
    source.write_text("External reference\n")
    bundle = reference_bundle.snapshot(project, [str(source)], "state/reference.json")
    assert bundle["sources"][0]["path"] == str(source.resolve())
    assert reference_bundle.check(project, "state/reference.json")["status"] == "pass"
    with pytest.raises(reference_bundle.ReferenceError, match="프로젝트 밖"):
        reference_bundle.snapshot(project, ["guide.md"], str(tmp_path / "escaped.json"))
    with pytest.raises(reference_bundle.ReferenceError, match="상대"):
        reference_bundle.snapshot(project, ["guide.md"], str(project / "absolute.json"))
    assert source.read_text() == "External reference\n"


@pytest.mark.parametrize("source", ["../outside.md", "guide.md\n", "guide.md\x00", "missing.md", "guide.md:stream"])
def test_rejects_invalid_or_missing_sources_without_output(project, source):
    with pytest.raises(reference_bundle.ReferenceError):
        reference_bundle.snapshot(project, [source], "state/reference.json")
    assert not (project / "state").exists()


def test_rejects_source_overwrite_duplicate_and_bad_metadata(project):
    for sources, output, metadata in [(["guide.md"], "guide.md", {}),
                                      (["guide.md", "./guide.md"], "bundle.json", {}),
                                      (["guide.md"], "bundle.json", {"revision": "x\ny"})]:
        with pytest.raises(reference_bundle.ReferenceError):
            reference_bundle.snapshot(project, sources, output, **metadata)
    assert (project / "guide.md").read_text() == "Original guidance\n"


@pytest.mark.parametrize("change", ["schema", "unknown", "source-unknown", "sha", "section", "duplicate", "traversal", "empty"])
def test_load_rejects_malformed_bundles(project, change):
    bundle = reference_bundle.snapshot(project, ["guide.md"], "bundle.json")
    source = bundle["sources"][0]
    if change == "schema":
        bundle["schema"] = True
    elif change == "unknown":
        bundle["extra"] = "value"
    elif change == "source-unknown":
        source["content"] = "unrequested copy"
    elif change == "sha":
        source["sha256"] = "not-a-hash"
    elif change == "section":
        source["section"] = "bad\tsection"
    elif change == "duplicate":
        bundle["sources"].append(dict(source))
    elif change == "traversal":
        source["path"] = "../outside.md"
    else:
        bundle["sources"] = []
    (project / "bundle.json").write_text(json.dumps(bundle))
    with pytest.raises(reference_bundle.ReferenceError):
        reference_bundle.load(project, "bundle.json")


def test_project_path_rejects_symlink_escape(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(reference_bundle.ReferenceError, match="프로젝트 밖"):
        reference_bundle.snapshot(project, ["guide.md"], "link/bundle.json")
    assert not (outside / "bundle.json").exists()


@pytest.mark.parametrize("output,content", [
    ("CLAUDE.md", "# Project guidance\n"),
    ("state/questions/confirm-A.json", '{"schema":1,"approval":{"status":"complete"}}'),
    ("state/mission.md", "# Mission\n"),
    ("plugin.json", '{"name":"example","version":"1.0"}'),
    ("source.py", "print('source')\n"),
])
def test_snapshot_never_overwrites_existing_non_bundle(project, output, content):
    destination = project / output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)
    expected = reference_bundle.file_pointer(project, output)["sha256"]
    for options in ({}, {"expected_sha256": expected}):
        with pytest.raises(reference_bundle.ReferenceError):
            reference_bundle.snapshot(project, ["guide.md"], output, **options)
        assert destination.read_text() == content


def test_bundle_refresh_requires_expected_hash_and_preserves_stale_update(project):
    reference_bundle.snapshot(project, ["guide.md"], "bundle.json")
    before = (project / "bundle.json").read_bytes()
    expected = reference_bundle.file_pointer(project, "bundle.json")["sha256"]
    (project / "guide.md").write_text("New current guidance\n")
    for options in ({}, {"expected_sha256": "0" * 64}):
        with pytest.raises(reference_bundle.ReferenceError, match="expect-sha256"):
            reference_bundle.snapshot(project, ["guide.md"], "bundle.json", **options)
        assert (project / "bundle.json").read_bytes() == before
    reference_bundle.snapshot(project, ["guide.md"], "bundle.json", expected_sha256=expected)
    assert reference_bundle.check(project, "bundle.json")["status"] == "pass"
    with pytest.raises(reference_bundle.ReferenceError, match="expect-sha256"):
        reference_bundle.snapshot(project, ["guide.md"], "new.json", expected_sha256=expected)
    assert not (project / "new.json").exists()


def test_concurrent_bundle_change_before_replace_is_preserved(project, monkeypatch):
    reference_bundle.snapshot(project, ["guide.md"], "bundle.json")
    expected = reference_bundle.file_pointer(project, "bundle.json")["sha256"]
    concurrent = (project / "bundle.json").read_bytes() + b"\n"
    original = reference_bundle.file_pointer
    def pointer(root, value, external=False):
        if value == "guide.md":
            (project / "bundle.json").write_bytes(concurrent)
        return original(root, value, external)
    monkeypatch.setattr(reference_bundle, "file_pointer", pointer)
    with pytest.raises(reference_bundle.ReferenceError, match="expect-sha256"):
        reference_bundle.snapshot(project, ["guide.md"], "bundle.json", expected_sha256=expected)
    assert (project / "bundle.json").read_bytes() == concurrent
    assert sorted(path.name for path in project.iterdir()) == ["bundle.json", "guide.md"]
