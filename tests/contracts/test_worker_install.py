"""The published plugin contains its executable worker contract and entry point."""

import json
import os
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
    home = tmp_path / "user home"
    (home / ".chef").mkdir(parents=True)
    (home / ".chef/config.json").write_text(json.dumps({"workers": {
        "runtimes": ["claude", "codex"], "roleRouting": {
            "code-reviewer": {"runtime": "claude"}, "reviewer-small": {"runtime": "claude"}}}}), encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.pop("CHEF_WORKERS_MODE", None)
    script = ("import sys,json; sys.path.insert(0,sys.argv[1]); from chef.workers import policy; "
              "print(json.dumps([policy.resolve({}, {}, owner_runtime='grok', role=role)['allowedRuntimes'] "
              "for role in ('dev-medium','code-reviewer','reviewer-small')]))")
    selected = subprocess.run([sys.executable, "-c", script, str(installed)], env=env,
                              cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert selected.returncode == 0, selected.stderr
    assert json.loads(selected.stdout) == [["claude", "codex"], ["claude"], ["claude"]]
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
