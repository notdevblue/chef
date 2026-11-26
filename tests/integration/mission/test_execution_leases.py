"""Public cooperative execution coordination, with wholly synthetic inputs."""

import json
import io
import contextlib
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from chef.guards import mission as guard
from chef.verification import execution
from chef.verification import plan as verification_plan
from chef.verification import runner


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    directory = tmp_path / "registry"
    monkeypatch.setattr(execution, "registry_directory", lambda: directory)
    return directory


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "example"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.txt").write_text("example input", encoding="utf-8")
    folder = root / "mission"
    folder.mkdir()
    plan = {"grade": "example-pass", "inputs": ["src"], "environment": {"target": "example"},
            "commands": [{"argv": [sys.executable, "-c", "pass"]}]}
    return root, folder, plan


@pytest.fixture
def mocked_file_symlink(project, monkeypatch):
    """Exercise DirEntry/realpath behavior where Windows denies real symlinks."""
    root, _, _ = project
    link = root / "src/link.txt"
    target = root / "common.txt"
    link.write_text("simulated link node", encoding="utf-8")
    target.write_text("example resolved target", encoding="utf-8")
    original_scan = execution.os.scandir
    original_canonical = execution.paths.canonical_path

    class LinkEntry:
        path = str(link)

        def is_dir(self, *, follow_symlinks=True):
            return False

        def is_symlink(self):
            return True

    @contextlib.contextmanager
    def scan(path):
        with original_scan(path) as entries:
            yield iter(LinkEntry() if Path(entry.path) == link else entry for entry in entries)

    monkeypatch.setattr(execution.os, "scandir", scan)
    monkeypatch.setattr(execution.paths, "canonical_path", lambda path: (
        original_canonical(target) if Path(path) == link else original_canonical(path)
    ))
    return root, link, target


def test_lease_blocks_file_parent_and_new_child_but_allows_disjoint_work(project, registry):
    root, _, _ = project
    with execution.input_lease([root / "src"]):
        for target in (root / "src/main.txt", root, root / "src/new.txt"):
            with pytest.raises(execution.VerificationError, match="protected execution input"):
                execution.require_mutation_allowed([target])
        execution.require_mutation_allowed([root / "other.txt"])
        # Readers can share inputs; a declared writer cannot.
        with execution.input_lease([root / "src/main.txt"]):
            pass
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            with execution.mutation_lease([root / "src"]):
                pytest.fail("overlapping mutation acquired")
    execution.require_mutation_allowed([root])
    assert json.loads((registry / "inputs.json").read_text())["leases"] == []
    assert (registry / "inputs.lock.gate").exists()


def test_declared_mutation_blocks_later_verify_until_it_finishes(project):
    root, _, _ = project
    with execution.mutation_lease([root / "src/main.txt"]):
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            with execution.input_lease([root / "src"]):
                pytest.fail("reader acquired during mutation")
        with execution.input_lease([root / "other.txt"]):
            pass
    with execution.input_lease([root / "src"]):
        pass


def test_file_hardlink_alias_is_protected(project):
    root, _, _ = project
    alias = root / "alias.txt"
    os.link(root / "src/main.txt", alias)
    with execution.input_lease([root / "src/main.txt"]):
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([alias])


def test_directory_alias_is_protected(project):
    root, _, _ = project
    linked = root / "linked"
    try:
        linked.symlink_to(root / "src", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink unavailable")
    with execution.input_lease([linked]):
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "src/new.txt"])


@pytest.mark.parametrize("existing", [True, False])
def test_real_file_symlink_target_matches_snapshot_and_is_protected(project, existing):
    root, _, _ = project
    target = root / "common.txt"
    if existing:
        target.write_text("example resolved input", encoding="utf-8")
    try:
        (root / "src/link.txt").symlink_to(target)
    except OSError:
        pytest.skip("file symlink unavailable")
    snapshot = verification_plan.snapshot(root, ["src"])
    assert snapshot["src/link.txt"] == (runner.file_hash(target) if existing else "missing")
    with execution.input_lease([root / "src"]) as lease:
        assert execution.paths.canonical_path(target) in lease["aliases"]
        assert "protected execution input" in guard.decide({
            "tool_name": "Write", "cwd": str(root), "tool_input": {"file_path": "common.txt"},
        }).deny
    execution.require_mutation_allowed([target])


def test_mocked_file_symlink_target_is_protected_without_hook_rescan(mocked_file_symlink, monkeypatch):
    root, _, target = mocked_file_symlink
    with execution.input_lease([root / "src"]) as lease:
        assert lease["aliases"] == [execution.paths.canonical_path(target)]

        def unexpected(*args, **kwargs):
            pytest.fail("known-target check rescanned symlink input tree")

        monkeypatch.setattr(execution.os, "scandir", unexpected)
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([target])
        assert "protected execution input" in guard.decide({
            "tool_name": "Write", "cwd": str(root), "tool_input": {"file_path": str(target)},
        }).deny
        execution.require_mutation_allowed([root / "unrelated.txt"])
    execution.require_mutation_allowed([target])


def test_file_symlink_index_cap_fails_before_lease_registration(mocked_file_symlink, registry, monkeypatch):
    root, _, _ = mocked_file_symlink
    monkeypatch.setattr(execution, "MAX_ALIASES", 0)
    with pytest.raises(execution.VerificationError, match="execution symlink index exceeds limit"):
        with execution.input_lease([root / "src"]):
            pytest.fail("unbounded symlink index")
    assert not (registry / "inputs.json").exists()


def test_alias_scan_and_registration_fence_declared_mutator_finishing_mid_scan(mocked_file_symlink, monkeypatch):
    root, link, target = mocked_file_symlink
    replacement = root / "replacement.txt"
    replacement.write_text("example replacement target", encoding="utf-8")
    state = threading.local()
    original_lock = execution.locking.FileLock
    original_index = execution.input_file_aliases
    original_canonical = execution.paths.canonical_path
    changed = []

    class ImmediateMutatorLock(original_lock):
        def __init__(self, path, label, timeout=0):
            super().__init__(path, label, timeout=0 if getattr(state, "mutator", False) else timeout)

    def mutate_alias():
        state.mutator = True
        try:
            with execution.mutation_lease([root / "src"]):
                monkeypatch.setattr(execution.paths, "canonical_path", lambda path: (
                    original_canonical(replacement) if Path(path) == link else original_canonical(path)
                ))
                changed.append(True)
        except (execution.locking.LockBusy, execution.VerificationError):
            return "blocked"
        return "changed"

    def index(targets):
        result = original_index(targets)
        if not getattr(state, "mutator", False):
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(mutate_alias).result(timeout=5) == "blocked"
        return result

    monkeypatch.setattr(execution.locking, "FileLock", ImmediateMutatorLock)
    monkeypatch.setattr(execution, "input_file_aliases", index)
    with execution.input_lease([root / "src"]) as lease:
        assert lease["aliases"] == [original_canonical(target)]
        assert not changed


def test_active_overlapping_mutation_is_rejected_before_alias_scan(project, monkeypatch):
    root, _, _ = project
    with execution.mutation_lease([root / "src"]):
        def unexpected(*args):
            pytest.fail("active overlapping mutation reached alias scan")

        monkeypatch.setattr(execution, "input_file_aliases", unexpected)
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            with execution.input_lease([root / "src"]):
                pytest.fail("active overlapping mutation allowed verify")


def test_directory_lease_protects_external_hardlink_without_rescanning(project, monkeypatch):
    root, _, _ = project
    alias = root / "outside-alias.txt"
    os.link(root / "src/main.txt", alias)
    with execution.input_lease([root / "src"]) as lease:
        assert len(lease["hardlinks"]) == 1

        def unexpected(*args, **kwargs):
            pytest.fail("known-target check rescanned input tree")

        monkeypatch.setattr(execution.os, "scandir", unexpected)
        for _ in range(2):
            with pytest.raises(execution.VerificationError, match="protected execution input hardlink"):
                execution.require_mutation_allowed([alias])
        assert "protected execution input hardlink" in guard.decide({
            "tool_name": "Write", "cwd": str(root), "tool_input": {"file_path": str(alias)},
        }).deny


def test_directory_identity_index_has_a_scan_limit(project, monkeypatch, registry):
    root, _, _ = project
    monkeypatch.setattr(execution, "MAX_SCAN_ENTRIES", 1)
    with pytest.raises(execution.VerificationError, match="identity scan exceeds limit"):
        with execution.input_lease([root / "src"]):
            pytest.fail("unbounded input scan")
    assert not (registry / "inputs.json").exists()


def test_case_sensitive_paths_remain_disjoint(project):
    root, _, _ = project
    other = root / "SRC"
    if other.exists():
        pytest.skip("case-insensitive volume")
    other.mkdir()
    with execution.input_lease([root / "src"]):
        execution.require_mutation_allowed([other / "new.txt"])


def test_alive_owner_is_not_expired_and_dead_owner_is_removed(project, registry, monkeypatch):
    root, _, _ = project
    with execution.input_lease([root / "src"]):
        path = registry / "inputs.json"
        value = json.loads(path.read_text())
        value["leases"][0]["startedAt"] = "1900-01-01T00:00:00Z"
        path.write_text(json.dumps(value))
        os.utime(path, (1, 1))
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "src/main.txt"])
        value["leases"][0]["pid"] = 2147483647
        value["leases"][0]["processStart"] = None
        path.write_text(json.dumps(value))
        real_alive = execution.locking.process_alive
        monkeypatch.setattr(execution.locking, "process_alive", lambda pid: False if pid == 2147483647 else real_alive(pid))
        execution.require_mutation_allowed([root / "src/main.txt"])
        assert json.loads(path.read_text())["leases"] == []


def test_reused_pid_does_not_keep_old_process_lease(project, registry, monkeypatch):
    root, _, _ = project
    monkeypatch.setattr(execution, "process_start", lambda pid: "first-incarnation")
    with execution.input_lease([root / "src"]):
        monkeypatch.setattr(execution, "process_start", lambda pid: "later-incarnation")
        execution.require_mutation_allowed([root / "src/main.txt"])
        assert json.loads((registry / "inputs.json").read_text())["leases"] == []


@pytest.mark.parametrize("value", ["{", "[]", "{}", '{"leases": "invalid"}',
                                       '{"leases":[{"mode":"verify","pid":0}]}',
                                       '{"leases":[{"mode":"verify","pid":999999999999999999999}]}'])
def test_corrupt_registry_fails_known_mutation_closed(project, registry, value):
    root, _, _ = project
    registry.mkdir()
    (registry / "inputs.json").write_text(value)
    outcome = guard.decide({"tool_name": "Write", "cwd": str(root),
                            "tool_input": {"file_path": "src/main.txt", "content": "new"}})
    assert "execution input protection" in outcome.deny
    assert "허용 파일:" not in outcome.deny


def test_registry_bounds_and_permission_errors_are_explicit(project, registry, monkeypatch):
    root, _, _ = project
    with execution.input_lease([root / "src"]):
        monkeypatch.setattr(execution, "MAX_BYTES", 1)
        with pytest.raises(execution.VerificationError, match="size limit"):
            execution.require_mutation_allowed([root / "src/main.txt"])
        monkeypatch.undo()
        monkeypatch.setattr(execution, "registry_directory", lambda: registry)
        read = execution.read_record

        def denied(path):
            if Path(path).name == "inputs.json":
                raise PermissionError("example denied registry")
            return read(path)

        monkeypatch.setattr(execution, "read_record", denied)
        outcome = guard.decide({"tool_name": "Edit", "cwd": str(root),
                                "tool_input": {"file_path": "src/main.txt"}})
        assert "example denied registry" in outcome.deny
        monkeypatch.setattr(execution, "read_record", read)


def test_deep_registry_json_is_denied_by_actual_hook_main(project, registry, monkeypatch):
    root, _, _ = project
    registry.mkdir()
    depth = sys.getrecursionlimit() + 100
    (registry / "inputs.json").write_text('{"nested":' * depth + '0' + '}' * depth)
    payload = {"tool_name": "Write", "cwd": str(root), "tool_input": {"file_path": "src/main.txt"}}
    output = io.StringIO()
    monkeypatch.setattr(guard.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(guard.sys, "stdout", output)
    monkeypatch.setattr(guard, "log_payload", lambda payload: None)
    assert guard.main() == 0
    result = json.loads(output.getvalue())["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"
    assert "execution registry nesting exceeds limit" in result["permissionDecisionReason"]


def test_registry_parser_recursion_error_is_wrapped(project, registry, monkeypatch):
    registry.mkdir()
    path = registry / "inputs.json"
    path.write_text('{"leases": []}')
    original = execution.json.loads

    def recursive(value, *args, **kwargs):
        if isinstance(value, bytes):
            raise RecursionError("example parser recursion")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(execution.json, "loads", recursive)
    with pytest.raises(execution.VerificationError, match="execution registry is unreadable"):
        execution.read_record(path)


@pytest.mark.parametrize("tool,command", [
    ("PowerShell", "Set-Content -LiteralPath 'src/[config].txt' -Value changed"),
    ("PowerShell", "Set-Content -LiteralPath src/[config].txt -Value changed"),
    ("Bash", "printf changed > 'src/[config].txt'"),
    ("Bash", 'printf changed > "src/[config].txt"'),
])
@pytest.mark.parametrize("existing", [True, False])
def test_literal_brackets_are_protected_for_existing_and_missing_paths(project, tool, command, existing):
    root, _, _ = project
    target = root / "src/[config].txt"
    if existing:
        target.write_text("example", encoding="utf-8")
    with execution.input_lease([target]):
        assert "protected execution input" in guard.decide({
            "tool_name": tool, "cwd": str(root), "tool_input": {"command": command},
        }).deny


def test_static_python_missing_literal_brackets_are_protected(project):
    root, _, _ = project
    target = root / "src/[config].txt"
    command = 'python -c "from pathlib import Path; Path(\'src/[config].txt\').write_text(\'changed\')"'
    with execution.input_lease([target]):
        assert "protected execution input" in guard.execution_write_reason({
            "tool_name": "Bash", "cwd": str(root), "tool_input": {"command": command},
        })


def test_powershell_new_name_brackets_are_literal_without_quotes(project):
    root, _, _ = project
    (root / "src/old.txt").write_text("example", encoding="utf-8")
    with execution.input_lease([root / "src/[new].txt"]):
        assert "protected execution input" in guard.execution_write_reason({
            "tool_name": "PowerShell", "cwd": str(root),
            "tool_input": {"command": "Rename-Item -LiteralPath src/old.txt -NewName [new].txt"},
        })


@pytest.mark.parametrize("tool,command", [
    ("Bash", "rm src/[ab].txt"),
    ("PowerShell", "Set-Content -Path 'src/[ab].txt' -Value changed"),
    ("PowerShell", "Rename-Item src/*/old.txt -NewName new.txt"),
])
def test_static_glob_uses_conservative_literal_parent_scope(project, tool, command):
    root, _, _ = project
    with execution.input_lease([root / "src/a.txt"]):
        assert "protected execution input" in guard.execution_write_reason({
            "tool_name": tool, "cwd": str(root), "tool_input": {"command": command},
        })


@pytest.mark.parametrize("tool,command", [
    ("Bash", 'python -c "from pathlib import Path; Path(\'src/[config].txt\').write_text(\'changed\')"'),
    ("Bash", "printf changed > 'src/[config].txt'"),
    ("PowerShell", "Set-Content -LiteralPath src/[config].txt -Value changed"),
    ("PowerShell", "Rename-Item -LiteralPath src/old.txt -NewName [new].txt"),
])
def test_proven_literal_brackets_do_not_expand_to_unrelated_siblings(project, tool, command):
    root, _, _ = project
    (root / "src/old.txt").write_text("example", encoding="utf-8")
    with execution.input_lease([root / "src/neighbor.txt"]):
        assert not guard.execution_write_reason({
            "tool_name": tool, "cwd": str(root), "tool_input": {"command": command},
        })


@pytest.mark.parametrize("command", [
    "Rename-Item src/old.txt -NewName new.txt",
    "Rename-Item -LiteralPath src/old.txt -NewName new.txt",
    "ren src/old.txt new.txt",
])
def test_rename_destination_is_in_source_directory(project, command):
    root, _, _ = project
    (root / "src/old.txt").write_text("example", encoding="utf-8")
    with execution.input_lease([root / "src/new.txt"]):
        assert "protected execution input" in guard.execution_write_reason({
            "tool_name": "PowerShell", "cwd": str(root), "tool_input": {"command": command},
        })
    with execution.input_lease([root / "new.txt"]):
        assert not guard.execution_write_reason({
            "tool_name": "PowerShell", "cwd": str(root), "tool_input": {"command": command},
        })
        assert "protected execution input" in guard.execution_write_reason({
            "tool_name": "PowerShell", "cwd": str(root),
            "tool_input": {"command": "Set-Content new.txt changed; " + command},
        })


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": "src/main.txt"}),
    ("Edit", {"file_path": "src/main.txt"}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Update File: src/main.txt\n*** End Patch"}),
    ("Bash", {"command": "printf changed > src/main.txt"}),
    ("Bash", {"command": "cd src && rm main.txt"}),
    ("PowerShell", {"command": "Set-Content -LiteralPath src/main.txt -Value changed"}),
    ("PowerShell", {"command": "Remove-Item -LiteralPath src -Recurse"}),
])
def test_known_hook_mutations_block_even_subagents_without_mission(project, tool, tool_input):
    root, _, _ = project
    with execution.input_lease([root / "src"]):
        outcome = guard.decide({"tool_name": tool, "tool_input": tool_input,
                                "cwd": str(root), "agent_id": "example-worker"})
        assert "protected execution input" in outcome.deny


def test_unknown_shell_scope_is_not_claimed_and_literal_other_target_is_allowed(project):
    root, _, _ = project
    with execution.input_lease([root / "src"]):
        for command in ("cd $unknown; rm main.txt", "printf changed > $unknown", "printf changed > other.txt"):
            assert not guard.execution_write_reason({"tool_name": "Bash", "cwd": str(root),
                                                      "tool_input": {"command": command}})


@pytest.mark.parametrize("failure", ["exit", "unexpected"])
def test_failed_run_releases_inputs_jobs_and_unit_lock(project, registry, monkeypatch, failure):
    root, folder, plan = project

    def launch(*args):
        execution.require_mutation_allowed([root / "other.txt"])
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "src/main.txt"])
        if failure == "unexpected":
            raise RuntimeError("example launch interrupted")
        return subprocess.CompletedProcess([], 2)

    monkeypatch.setattr(runner, "execute_command", launch)
    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="example launch interrupted"):
            runner.run(root, folder, "M1", plan, lambda command: None)
    else:
        assert runner.run(root, folder, "M1", plan, lambda command: None)[1]["status"] == "fail"
    assert json.loads((registry / "inputs.json").read_text())["leases"] == []
    assert execution.active_verification(folder, "M1") is None
    with execution.unit_execution_lock(folder, "M1"):
        pass


def test_snapshot_is_taken_after_input_lease_and_resource_lock(project, monkeypatch):
    root, folder, plan = project
    settings = {"resources": {"example-pass": ["example-engine"]}, "resourceDir": str(root / "locks")}
    original = runner.snapshot

    def snapshot(project, inputs):
        assert (root / "locks/example-engine.lock").exists()
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "src/main.txt"])
        return original(project, inputs)

    monkeypatch.setattr(runner, "snapshot", snapshot)
    monkeypatch.setattr(verification_plan, "snapshot", snapshot)
    assert runner.run(root, folder, "M1", plan, lambda command: None, settings=settings)[1]["status"] == "pass"


def test_concurrent_duplicate_references_owner_and_contract_mutation_is_blocked(project, monkeypatch):
    root, folder, plan = project
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def launch(*args):
        calls.append(1)
        entered.set()
        assert finish.wait(10)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(runner, "execute_command", launch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.run, root, folder, "M1", plan, lambda command: None)
        try:
            assert entered.wait(10)
            original = execution.active_verification(folder, "M1")
            for request in (plan, {**plan, "environment": {"target": "changed"}}):
                with pytest.raises(execution.ExecutionBusy) as busy:
                    runner.run(root, folder, "M1", request, lambda command: None)
                assert busy.value.record["token"] == original["token"]
                assert busy.value.record["sameRequest"] == (request == plan)
            with pytest.raises(execution.ExecutionBusy):
                with execution.unit_execution_lock(folder, "M1"):
                    pytest.fail("contract mutation acquired during run")
            assert len(calls) == 1
        finally:
            finish.set()
        first = future.result(timeout=10)
    second = runner.run(root, folder, "M1", plan, lambda command: None)
    assert second[2] and second[0] == first[0]
    assert len(calls) == 1


def test_review_reservation_is_idempotent_and_keyed_by_all_review_inputs(project):
    _, folder, _ = project
    args = (folder, "M1", "snapshot-one", "independent-review", ["a.txt", "b.txt"], [{"id": "finding-one"}])
    first = execution.reserve_review(*args)
    second = execution.reserve_review(folder, "M1", "snapshot-one", "independent-review",
                                       ["b.txt", "a.txt", "a.txt"], [{"id": "finding-one"}])
    assert first["acquired"] and not second["acquired"]
    assert first["key"] == second["key"] and first["token"] == second["token"]
    with pytest.raises(execution.VerificationError, match="no longer owned"):
        execution.finish_job(folder, first["key"], "wrong-token", {"evidence": "review.json"})
    execution.finish_job(folder, first["key"], first["token"], {"evidence": "review.json"})
    completed = execution.reserve_review(*args)
    assert completed["status"] == "completed" and not completed["acquired"]
    assert completed["result"] == {"evidence": "review.json"}
    for index, changed in ((1, "M2"), (2, "snapshot-two"), (3, "follow-up"),
                           (4, ["a.txt"]), (5, [{"id": "finding-two"}])):
        altered = list(args)
        altered[index] = changed
        other = execution.reserve_review(*altered)
        assert other["acquired"] and other["key"] != first["key"]


def test_dead_review_owner_replaced_and_old_token_cannot_complete(project, monkeypatch):
    _, folder, _ = project
    with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as worker:
        first = execution.reserve_review(folder, "M1", "snapshot", "review", [], [], owner_pid=worker.pid)
        old = execution.read_record(execution.job_path(execution.job_directory(folder), first["key"]))
        old["startedAt"] = "1900-01-01T00:00:00Z"
        execution.write_record(execution.job_path(execution.job_directory(folder), first["key"]), old)
        assert not execution.reserve_review(folder, "M1", "snapshot", "review", [], [])["acquired"]
        worker.stdin.close()
        worker.wait(timeout=5)
    later = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    assert later["acquired"] and later["token"] != first["token"]
    with pytest.raises(execution.VerificationError, match="no longer owned"):
        execution.finish_job(folder, first["key"], first["token"], {})
    execution.finish_job(folder, later["key"], later["token"], release=True)
    assert execution.reserve_review(folder, "M1", "snapshot", "review", [], [])["acquired"]


def test_worker_claim_transfers_pid_and_fences_delayed_old_worker(project):
    _, folder, _ = project
    first = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as worker:
        transferred = execution.transfer_job(folder, first["key"], first["token"], worker.pid)
        assert transferred["pid"] == worker.pid
        assert transferred["processStart"] == execution.process_start(worker.pid)
        with pytest.raises(execution.VerificationError, match="already been claimed"):
            execution.transfer_job(folder, first["key"], first["token"], os.getpid())
        assert not execution.reserve_review(folder, "M1", "snapshot", "review", [], [])["acquired"]
        worker.stdin.close()
        worker.wait(timeout=5)
    later = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    assert later["acquired"]
    with pytest.raises(execution.VerificationError, match="no longer owned"):
        execution.transfer_job(folder, first["key"], first["token"], os.getpid())


def test_uncertain_launch_is_not_reclaimed_after_owner_death(project, registry, monkeypatch):
    root, folder, _ = project
    job = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    with execution.input_lease([root / "src"]) as lease:
        with execution.unit_execution_lock(folder, "M1", owned_job=job):
            execution.begin_job_launch(folder, job["key"], job["token"], lease["token"])
        inputs_path = registry / "inputs.json"
        inputs = execution.read_record(inputs_path)
        inputs["leases"][0].update(pid=2147483647, processStart=None)
        execution.write_record(inputs_path, inputs)
        job_path = execution.job_path(execution.job_directory(folder), job["key"])
        record = execution.read_record(job_path)
        record.update(pid=2147483647, processStart=None)
        execution.write_record(job_path, record)
        real_alive = execution.locking.process_alive
        monkeypatch.setattr(execution.locking, "process_alive", lambda pid: False if pid == 2147483647 else real_alive(pid))
        assert not execution.reserve_review(folder, "M1", "snapshot", "review", [], [])["acquired"]
        with pytest.raises(execution.ExecutionBusy):
            with execution.unit_execution_lock(folder, "M1"):
                pytest.fail("uncertain launch allowed contract mutation")
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "src/main.txt"])
        with pytest.raises(execution.VerificationError, match="launch needs recovery"):
            execution.finish_job(folder, job["key"], job["token"], release=True)
    assert execution.read_record(inputs_path)["leases"][0]["launchPending"]


def test_live_child_keeps_dead_supervisor_job_inputs_and_contract_protected(project, registry, monkeypatch):
    root, folder, _ = project
    job = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    with execution.input_lease([root / "src"]) as lease:
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            execution.begin_job_launch(folder, job["key"], job["token"], lease["token"])
            execution.register_job_child(folder, job["key"], job["token"], lease["token"], child.pid)
            inputs_path = registry / "inputs.json"
            inputs = execution.read_record(inputs_path)
            inputs["leases"][0].update(pid=2147483647, processStart=None)
            execution.write_record(inputs_path, inputs)
            job_path = execution.job_path(execution.job_directory(folder), job["key"])
            record = execution.read_record(job_path)
            record.update(pid=2147483647, processStart=None)
            execution.write_record(job_path, record)
            real_alive = execution.locking.process_alive
            monkeypatch.setattr(execution.locking, "process_alive", lambda pid: False if pid == 2147483647 else real_alive(pid))
            assert not execution.reserve_review(folder, "M1", "snapshot", "review", [], [])["acquired"]
            with pytest.raises(execution.ExecutionBusy):
                with execution.unit_execution_lock(folder, "M1"):
                    pytest.fail("live child allowed contract mutation")
            with pytest.raises(execution.VerificationError, match="protected execution input"):
                execution.require_mutation_allowed([root / "src/main.txt"])
            with pytest.raises(execution.VerificationError, match="execution child is live"):
                execution.finish_job(folder, job["key"], job["token"], release=True)
            child.stdin.close()
            child.wait(timeout=5)
        execution.require_mutation_allowed([root / "src/main.txt"])
        later = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
        assert later["acquired"] and later["token"] != job["token"]


def test_reaped_child_can_finish_and_release_inputs(project):
    root, folder, _ = project
    job = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    with execution.input_lease([root / "src"]) as lease:
        execution.begin_job_launch(folder, job["key"], job["token"], lease["token"])
        with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
            execution.register_job_child(folder, job["key"], job["token"], lease["token"], child.pid)
            child.wait(timeout=5)
        execution.cancel_job_launch(folder, job["key"], job["token"], lease["token"])
        execution.finish_job(folder, job["key"], job["token"], {"status": "completed"})
    execution.require_mutation_allowed([root / "src/main.txt"])


def test_cancel_launch_cannot_drop_registered_live_child(project):
    root, folder, _ = project
    job = execution.reserve_review(folder, "M1", "snapshot", "review", [], [])
    with execution.input_lease([root / "src"]) as lease:
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            execution.begin_job_launch(folder, job["key"], job["token"], lease["token"])
            execution.register_job_child(folder, job["key"], job["token"], lease["token"], child.pid)
            with pytest.raises(execution.VerificationError, match="execution child is still live"):
                execution.cancel_job_launch(folder, job["key"], job["token"], lease["token"])
            current = execution.read_record(execution.job_path(execution.job_directory(folder), job["key"]))
            assert execution.child_alive(current)
            with pytest.raises(execution.VerificationError, match="protected execution input"):
                execution.require_mutation_allowed([root / "src/main.txt"])
            child.stdin.close()
            child.wait(timeout=5)
        execution.cancel_job_launch(folder, job["key"], job["token"], lease["token"])
        execution.finish_job(folder, job["key"], job["token"], {})


def test_verify_parent_kill_preserves_real_child_input_unit_and_resource_ownership(project, registry):
    root, folder, _ = project
    source = """
import sys
from pathlib import Path
from chef.verification import execution, runner
root, folder, registry = map(Path, sys.argv[1:])
execution.registry_directory = lambda: registry
register = execution.register_job_child
def child_started(*args):
    result = register(*args)
    print(args[-1], flush=True)
    return result
execution.register_job_child = child_started
plan = {'grade': 'example-pass', 'inputs': ['src'], 'environment': {'target': 'example'},
        'commands': [{'argv': [sys.executable, '-c', 'import threading; threading.Event().wait(30)']}]}
settings = {'resourceDir': str(root / 'locks'), 'resources': {'example-pass': ['example-engine']}}
runner.run(root, folder, 'M1', plan, lambda command: None, settings=settings)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(execution.__file__).resolve().parents[2])}
    parent = subprocess.Popen([sys.executable, "-c", source, str(root), str(folder), str(registry)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    child_pid = None
    try:
        line = parent.stdout.readline().strip()
        assert line.isdigit(), parent.stderr.read()
        child_pid = int(line)
        parent.kill()
        parent.wait(timeout=5)
        current = execution.active_verification(folder, "M1")
        assert current["child"]["pid"] == child_pid and execution.child_alive(current)
        with pytest.raises(execution.VerificationError, match="protected execution input"):
            execution.require_mutation_allowed([root / "src/main.txt"])
        with pytest.raises(execution.ExecutionBusy) as busy:
            with execution.unit_execution_lock(folder, "M1"):
                pytest.fail("dead verify parent allowed another unit writer")
        assert busy.value.record["child"]["pid"] == child_pid
        with pytest.raises(execution.locking.LockBusy):
            with runner.ResourceLock(root / "locks/example-engine.lock", "example-engine"):
                pytest.fail("dead verify parent released a running child resource")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        parent.stdout.close()
        parent.stderr.close()
