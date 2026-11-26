"""The published plugin contains its executable worker contract and entry point."""

import re
import shutil
import subprocess
import sys
from pathlib import Path


def test_worker_contract_is_available_in_copied_install(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    installed = tmp_path / "installed plugin"
    shutil.copytree(repository / "plugins/chef", installed,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    result = subprocess.run([sys.executable, str(installed / "worker.py"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "native-result" in result.stdout and "reconcile" in result.stdout
    entries = ("shared/skills/mission.md", "skills/mission/SKILL.md",
               "codex/skills/chef-mission/SKILL.md", "grok/skills/chef-mission/SKILL.md")
    for entry in entries:
        source = installed / entry
        links = re.findall(r"\]\(([^)]+cross-runtime-workers\.md(?:#[^)]*)?)\)",
                           source.read_text(encoding="utf-8"))
        assert links, f"worker routing instructions missing in {entry}"
        for target in links:
            resolved = (source.parent / target.split("#", 1)[0]).resolve()
            assert resolved.is_relative_to(installed)
            assert resolved.is_file(), f"deployed contract missing: {entry} -> {target}"
