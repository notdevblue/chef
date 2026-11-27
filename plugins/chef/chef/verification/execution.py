"""Cooperative input leases and idempotent execution reservations.

These records coordinate instrumented Chef callers, not arbitrary filesystem
writes. Tool hooks perform a pre-use check; callers needing an atomic reservation
for their entire mutation must use ``mutation_lease``. Hash checks after execution
remain necessary. All short registry transactions use the permanent FileLock gate.
"""

import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from contextvars import ContextVar

from chef.infrastructure import locking, paths
from chef.infrastructure.storage import atomic_write_json
from chef.verification.contracts import VerificationError, digest, utc_now


MAX_RECORDS = 2048
MAX_PATHS = 4096
MAX_BYTES = 4 * 1024 * 1024
MAX_SCAN_ENTRIES = 250000
MAX_HARDLINKS = 16384
MAX_ALIASES = 16384
MAX_JSON_DEPTH = 64
COMMAND_CONTEXT = ContextVar("chef_execution_command", default=None)
REGISTRY_CONTEXT = ContextVar("chef_execution_registry", default=None)


class ExecutionBusy(VerificationError):
    def __init__(self, record):
        self.record = record
        super().__init__(
            f"execution already owned: {record['key']} (pid {record['pid']}, token {record['token']})",
            "execution_busy",
        )


def registry_directory():
    """A workspace scope is shared by runtimes, never by unrelated projects."""
    if REGISTRY_CONTEXT.get() is not None:
        return Path(REGISTRY_CONTEXT.get())
    return Path.home() / ".cache" / "chef" / "execution"


def workspace_root(cwd):
    root = Path(cwd).resolve()
    for candidate in (root, *root.parents):
        if any((candidate / marker).exists() for marker in (".p4config", ".git", ".chef/binding.json",
                   ".claude/orchestrator/adapter.json", ".codex/orchestrator/adapter.json",
                   ".grok/orchestrator/adapter.json", ".hermes/orchestrator/adapter.json")):
            return candidate
    return root


def legacy_leases(directory):
    """Read old atomic records during upgrade without taking the global gate."""
    legacy = Path.home() / ".cache" / "chef" / "execution"
    if not Path(directory).is_relative_to(legacy / "workspaces") or not (legacy / "inputs.json").is_file():
        return []
    return live_leases(legacy)


@contextlib.contextmanager
def workspace_scope(project):
    if REGISTRY_CONTEXT.get() is not None:
        yield
        return
    root = Path(project).resolve()
    info = root.stat()
    key = digest([info.st_dev, info.st_ino] if info.st_ino else str(root))
    token = REGISTRY_CONTEXT.set(registry_directory() / "workspaces" / key)
    try:
        yield
    finally:
        REGISTRY_CONTEXT.reset(token)


def read_record(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_BYTES:
        raise VerificationError("execution registry exceeds size limit", "execution_registry_invalid")
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise VerificationError("execution registry nesting exceeds limit", "execution_registry_invalid")
        elif byte in (93, 125):
            depth -= 1
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError, MemoryError) as error:
        raise VerificationError("execution registry is unreadable", "execution_registry_invalid") from error
    if not isinstance(value, dict):
        raise VerificationError("execution registry must be an object", "execution_registry_invalid")
    return value


def write_record(path, value):
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError, MemoryError) as error:
        raise VerificationError("execution registry cannot be encoded", "execution_registry_invalid") from error
    if size > MAX_BYTES:
        raise VerificationError("execution registry exceeds size limit", "execution_registry_invalid")
    atomic_write_json(path, value, create_parents=True)


def owner_alive(record):
    if record.get("launchPending") is True or child_alive(record):
        return True
    return process_owner_alive(record)


def child_alive(record):
    child = record.get("child")
    if child is None:
        return False
    if not isinstance(child, dict):
        raise VerificationError("execution child identity is invalid", "execution_registry_invalid")
    return process_owner_alive(child)


def process_owner_alive(record):
    pid = record.get("pid")
    if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
        # Invalid ownership cannot be safely reclaimed based on age.
        raise VerificationError("execution registry owner is invalid", "execution_registry_invalid")
    try:
        if not locking.process_alive(pid):
            return False
    except (OSError, OverflowError, ValueError) as error:
        raise VerificationError("execution registry owner cannot be checked", "execution_registry_invalid") from error
    expected = record.get("processStart")
    if expected is not None and not isinstance(expected, str):
        raise VerificationError("execution process identity is invalid", "execution_registry_invalid")
    observed = process_start(pid)
    return not expected or not observed or expected == observed


def process_start(pid):
    """Best-effort process incarnation; unavailable identity keeps a live owner."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        try:
            if kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    elif sys.platform.startswith("linux"):
        try:
            # comm is parenthesized and can contain spaces or ')'.
            tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return tail[19]  # field 22, relative to field 3 (state)
        except (OSError, IndexError, UnicodeError):
            pass
    return None


def canonical_inputs(values):
    if not isinstance(values, (list, tuple)) or not values or len(values) > MAX_PATHS:
        raise VerificationError("execution inputs must be a bounded nonempty path array", "plan_invalid")
    result = []
    seen_paths, seen_files = set(), set()
    for value in values:
        if not isinstance(value, (str, os.PathLike)) or not isinstance(os.fspath(value), str) or not os.fspath(value):
            raise VerificationError("execution input path is invalid", "plan_invalid")
        path = paths.canonical_path(value)
        try:
            stat = os.stat(path)
            identity = (stat.st_dev, stat.st_ino) if stat.st_ino else None
        except FileNotFoundError:
            identity = None
        if path in seen_paths or (identity is not None and identity in seen_files):
            continue
        seen_paths.add(path)
        if identity is not None:
            seen_files.add(identity)
        result.append(path)
    return sorted(result)


def overlaps(first, second):
    return (
        paths.same_path(first, second)
        or paths.inside_real_root(first, second)
        or paths.inside_real_root(second, first)
    )


def input_file_aliases(targets):
    """Index hardlink identities and file symlink targets once per lease.

    Ordinary hook checks stat only the known mutation target and consult this
    index. They never recursively hash or rescan a leased input tree.
    """
    identities = set()
    aliases = set()
    seen = 0
    for target in targets:
        pending = [target]
        while pending:
            current = pending.pop()
            seen += 1
            if seen > MAX_SCAN_ENTRIES:
                raise VerificationError("execution input identity scan exceeds limit", "execution_registry_invalid")
            try:
                info = os.stat(current)
            except FileNotFoundError:
                continue
            if os.path.isdir(current):
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_symlink():
                            # snapshot() reads file links through their resolved
                            # target, including missing targets, but does not
                            # recursively traverse directory links.
                            if not entry.is_dir():
                                aliases.add(paths.canonical_path(entry.path))
                                if len(aliases) > MAX_ALIASES:
                                    raise VerificationError("execution symlink index exceeds limit", "execution_registry_invalid")
                                pending.append(entry.path)
                            else:
                                seen += 1
                        else:
                            pending.append(entry.path)
                        if len(pending) + seen > MAX_SCAN_ENTRIES:
                            raise VerificationError("execution input identity scan exceeds limit", "execution_registry_invalid")
            elif info.st_nlink > 1 and info.st_ino:
                identities.add((info.st_dev, info.st_ino))
                if len(identities) > MAX_HARDLINKS:
                    raise VerificationError("execution hardlink index exceeds limit", "execution_registry_invalid")
    return sorted(identities), sorted(aliases)


def live_leases(directory):
    value = read_record(directory / "inputs.json")
    if value is None:
        value = {"leases": []}
    records = value.get("leases")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise VerificationError("execution lease registry is invalid", "execution_registry_invalid")
    live = []
    for record in records:
        if not isinstance(record, dict) or record.get("mode") not in ("verify", "mutation"):
            raise VerificationError("execution lease record is invalid", "execution_registry_invalid")
        if owner_alive(record):
            if not isinstance(record.get("token"), str) or not record["token"] or not isinstance(record.get("paths"), list):
                raise VerificationError("execution lease record is invalid", "execution_registry_invalid")
            if not 1 <= len(record["paths"]) <= MAX_PATHS or not all(isinstance(p, str) and p for p in record["paths"]):
                raise VerificationError("execution lease paths are invalid", "execution_registry_invalid")
            links = record.get("hardlinks", [])
            if (
                not isinstance(links, list) or len(links) > MAX_HARDLINKS
                or any(not isinstance(item, (list, tuple)) or len(item) != 2
                       or any(type(value) is not int or value < 0 for value in item) for item in links)
            ):
                raise VerificationError("execution hardlink index is invalid", "execution_registry_invalid")
            aliases = record.get("aliases", [])
            if (
                not isinstance(aliases, list) or len(aliases) > MAX_ALIASES
                or not all(isinstance(path, str) and path for path in aliases)
            ):
                raise VerificationError("execution symlink index is invalid", "execution_registry_invalid")
            live.append(record)
    return live


def require_no_overlap(records, targets, mode, hardlinks=()):
    target_links = set(hardlinks)
    for target in targets:
        try:
            info = os.stat(target)
        except FileNotFoundError:
            continue
        if info.st_nlink > 1 and info.st_ino and not os.path.isdir(target):
            target_links.add((info.st_dev, info.st_ino))
    for record in records:
        if record.get("indexing") and (mode == "mutation" or record["mode"] == "mutation"):
            raise VerificationError("protected execution input: workspace input index is being prepared", "input_busy")
        if mode == "verify" and record["mode"] == "verify":
            continue
        if target_links.intersection(tuple(item) for item in record.get("hardlinks", [])):
            raise VerificationError(
                f"protected execution input hardlink: {targets[0]} (pid {record['pid']}, lease {record['token']})",
                "input_busy",
            )
        for target in targets:
            if any(overlaps(target, protected) for protected in [*record["paths"], *record.get("aliases", [])]):
                raise VerificationError(
                    f"protected execution input: {target} (pid {record['pid']}, lease {record['token']})",
                    "input_busy",
                )


@contextlib.contextmanager
def input_lease(values, *, mode="verify", directory=None, label=""):
    if mode not in ("verify", "mutation"):
        raise ValueError("unknown execution lease mode")
    directory = Path(directory) if directory is not None else registry_directory()
    lock = directory / "inputs.lock"
    targets = canonical_inputs(values)
    with locking.FileLock(lock, "execution inputs", timeout=2):
        records = live_leases(directory)
        require_no_overlap([*records, *legacy_leases(directory)], targets, mode)
        if len(records) >= MAX_RECORDS:
            raise VerificationError("execution lease registry is full", "execution_registry_invalid")
        record = {"token": uuid.uuid4().hex, "pid": os.getpid(), "processStart": process_start(os.getpid()), "mode": mode,
                  "paths": targets, "hardlinks": [], "aliases": [], "indexing": True, "label": str(label), "startedAt": utc_now()}
        write_record(directory / "inputs.json", {"leases": [*records, record]})
    scope = REGISTRY_CONTEXT.set(directory)
    indexed = False
    try:
        links, aliases = input_file_aliases(targets)
        with locking.FileLock(lock, "execution inputs", timeout=2):
            records = [item for item in live_leases(directory) if item["token"] != record["token"]]
            require_no_overlap([*records, *legacy_leases(directory)], [*targets, *aliases], mode, links)
            record.update(hardlinks=links, aliases=aliases, indexing=False)
            write_record(directory / "inputs.json", {"leases": [*records, record]})
            indexed = True
        yield record
    finally:
        try:
            with locking.FileLock(lock, "execution inputs", timeout=2):
                records = live_leases(directory)
                remaining = [item for item in records if item["token"] != record["token"]
                             or item.get("launchPending") is True or child_alive(item)]
                if not indexed and not remaining:
                    (directory / "inputs.json").unlink(missing_ok=True)
                else:
                    write_record(directory / "inputs.json", {"leases": remaining})
        finally:
            REGISTRY_CONTEXT.reset(scope)


def mutation_lease(values, **kwargs):
    return input_lease(values, mode="mutation", **kwargs)


def require_mutation_allowed(values, *, directory=None):
    """Bounded pre-use check for known targets; it does not reserve tool lifetime."""
    directory = Path(directory) if directory is not None else registry_directory()
    try:
        os.stat(directory / "inputs.json")
    except FileNotFoundError:
        require_no_overlap(legacy_leases(directory), canonical_inputs(values), "mutation")
        return
    targets = canonical_inputs(values)
    with locking.FileLock(directory / "inputs.lock", "execution inputs", timeout=2):
        records = live_leases(directory)
        require_no_overlap([*records, *legacy_leases(directory)], targets, "mutation")
        write_record(directory / "inputs.json", {"leases": records})


def has_input_leases():
    """Cheap bounded read before shell target extraction; corrupt records deny."""
    directory = registry_directory()
    return bool(live_leases(directory) or legacy_leases(directory))


def job_directory(folder):
    return Path(folder).resolve() / "verification" / "jobs"


def job_path(directory, key):
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise VerificationError("execution job key is invalid", "execution_registry_invalid")
    return directory / f"{key}.json"


def reserve_job(folder, purpose, identity, *, owner_pid=None, restart_completed=False, request_identity=None, unit=None):
    """Reserve one identity. Only ``acquired=True`` authorizes a new dispatch.

    A CLI caller must pass the long-lived worker/coordinator PID as owner_pid.
    A completed reservation is reused unless restart_completed is explicit.
    Dead running owners can be replaced; alive owners are never expired by age.
    """
    if not isinstance(purpose, str) or not purpose:
        raise VerificationError("execution purpose is required", "plan_invalid")
    pid = os.getpid() if owner_pid is None else owner_pid
    if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF or not locking.process_alive(pid):
        raise VerificationError("execution owner PID must be alive", "execution_owner_invalid")
    directory = job_directory(folder)
    key = digest({"purpose": purpose, "identity": identity})
    path = job_path(directory, key)
    with locking.FileLock(directory / "registry.lock", "execution jobs", timeout=2):
        previous = read_record(path)
        if previous is not None:
            if (
                previous.get("key") != key
                or previous.get("status") not in ("running", "completed")
                or not isinstance(previous.get("token"), str)
                or not previous["token"]
                or type(previous.get("pid")) is not int
                or previous["pid"] <= 0
            ):
                raise VerificationError("execution job record is invalid", "execution_registry_invalid")
            if previous["status"] == "completed" and not restart_completed:
                return {**previous, "acquired": False}
            if previous["status"] == "running" and owner_alive(previous):
                return {**previous, "acquired": False}
        if previous is None:
            # Bound persistent review reservations without scanning arbitrary trees.
            count = 0
            for _ in directory.glob("*.json"):
                count += 1
                if count >= MAX_RECORDS:
                    raise VerificationError("execution job registry is full", "execution_registry_invalid")
        record = {"key": key, "token": uuid.uuid4().hex, "pid": pid, "processStart": process_start(pid), "purpose": purpose,
                  "status": "running", "startedAt": utc_now(), "result": None}
        if request_identity is not None:
            record["requestIdentity"] = request_identity
        if unit is not None:
            record["unit"] = unit
        write_record(path, record)
        return {**record, "acquired": True}


def finish_job(folder, key, token, result=None, *, release=False):
    """Token-fenced completion/release, including from a separate CLI process."""
    directory = job_directory(folder)
    path = job_path(directory, key)
    with locking.FileLock(directory / "registry.lock", "execution jobs", timeout=2):
        record = read_record(path)
        if not record or record.get("token") != token or record.get("status") != "running":
            raise VerificationError("execution job is no longer owned", "execution_owner_invalid")
        if record.get("launchPending") is True or child_alive(record):
            raise VerificationError("execution child is live or launch needs recovery", "execution_busy")
        if release:
            path.unlink()
            return {**record, "status": "released"}
        record.update(status="completed", result=result, finishedAt=utc_now())
        write_record(path, record)
        return record


def transfer_job(folder, key, token, owner_pid):
    """A worker must claim its current token before launching expensive work.

    The old owner may already have exited. A competing reservation then changes
    the token, fencing off a delayed worker before it can launch its tool.
    """
    if type(owner_pid) is not int or not 0 < owner_pid <= 0x7FFFFFFF or not locking.process_alive(owner_pid):
        raise VerificationError("execution owner PID must be alive", "execution_owner_invalid")
    directory = job_directory(folder)
    path = job_path(directory, key)
    with locking.FileLock(directory / "registry.lock", "execution jobs", timeout=2):
        record = read_record(path)
        if not record or record.get("token") != token or record.get("status") != "running":
            raise VerificationError("execution job is no longer owned", "execution_owner_invalid")
        if record.get("transferredAt"):
            raise VerificationError("execution job has already been claimed", "execution_owner_invalid")
        record.update(pid=owner_pid, processStart=process_start(owner_pid), transferredAt=utc_now())
        write_record(path, record)
        return record


def update_launch_records(folder, key, token, lease_token, update, *, require_child_exit=False):
    """Persist input protection before job ownership at every launch boundary."""
    directory = registry_directory()
    with locking.FileLock(directory / "inputs.lock", "execution inputs", timeout=2):
        records = live_leases(directory)
        lease = next((item for item in records if item["token"] == lease_token), None)
        if lease is None or lease.get("pid") != os.getpid():
            raise VerificationError("execution input lease is no longer owned", "execution_owner_invalid")
        if require_child_exit and child_alive(lease):
            raise VerificationError("execution child is still live", "execution_busy")
        lease.update(update)
        write_record(directory / "inputs.json", {"leases": records})
    directory = job_directory(folder)
    path = job_path(directory, key)
    with locking.FileLock(directory / "registry.lock", "execution jobs", timeout=2):
        record = read_record(path)
        if (
            not record or record.get("token") != token or record.get("status") != "running"
            or record.get("pid") != os.getpid()
        ):
            raise VerificationError("execution job is no longer owned", "execution_owner_invalid")
        if require_child_exit and child_alive(record):
            raise VerificationError("execution child is still live", "execution_busy")
        record.update(update)
        write_record(path, record)
        return record


def begin_job_launch(folder, key, token, lease_token):
    """Fence a spawn/registration crash window; uncertain launches need recovery."""
    return update_launch_records(folder, key, token, lease_token, {"launchPending": True, "child": None})


def register_job_child(folder, key, token, lease_token, child_pid):
    if type(child_pid) is not int or not 0 < child_pid <= 0x7FFFFFFF:
        raise VerificationError("execution child PID is invalid", "execution_owner_invalid")
    return update_launch_records(folder, key, token, lease_token, {
        "launchPending": False, "child": {"pid": child_pid, "processStart": process_start(child_pid)},
    })


def cancel_job_launch(folder, key, token, lease_token):
    """Only after the caller proves no child started, or has reaped that child."""
    return update_launch_records(folder, key, token, lease_token, {"launchPending": False, "child": None},
                                 require_child_exit=True)


@contextlib.contextmanager
def track_commands(folder, job, lease):
    token = COMMAND_CONTEXT.set((folder, job, lease))
    try:
        yield
    finally:
        COMMAND_CONTEXT.reset(token)


class TrackedSubprocess:
    """The runner's existing subprocess.run seam, with child lifetime tracking."""

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, *popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
        context = COMMAND_CONTEXT.get()
        if context is None:
            return subprocess.run(*popenargs, input=input, capture_output=capture_output,
                                  timeout=timeout, check=check, **kwargs)
        folder, job, lease = context
        if input is not None:
            if kwargs.get("stdin") is not None:
                raise ValueError("stdin and input arguments may not both be used")
            kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                raise ValueError("stdout/stderr may not be used with capture_output")
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        begin_job_launch(folder, job["key"], job["token"], lease["token"])
        child = None
        reaped = False
        no_child_started = False
        try:
            try:
                child = subprocess.Popen(*popenargs, **kwargs)
            except OSError:
                no_child_started = True
                raise
            register_job_child(folder, job["key"], job["token"], lease["token"], child.pid)
            try:
                stdout, stderr = child.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                child.kill()
                stdout, stderr = child.communicate()
                reaped = True
                error.output, error.stderr = stdout, stderr
                raise
            reaped = True
            result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
            if check:
                result.check_returncode()
            return result
        except BaseException:
            if child is not None and not reaped:
                child.kill()
                child.communicate()
                reaped = True
            raise
        finally:
            if no_child_started or reaped:
                cancel_job_launch(folder, job["key"], job["token"], lease["token"])


tracked_subprocess = TrackedSubprocess()


def reserve_review(folder, unit, snapshot, purpose, scope, findings, *, owner_pid=None):
    """Idempotent supported dispatch path, keyed by all review inputs.

    ``scope`` and ``findings`` are sets of JSON values; ordering does not cause
    another review. The caller must check acquired before launching its worker
    and finish_job with the token afterward. This does not intercept native
    runtime dispatch APIs that do not call this helper.
    """
    def ordered(values):
        return sorted({json.dumps(value, sort_keys=True, ensure_ascii=False) for value in values})

    if (
        not isinstance(scope, (list, tuple)) or not isinstance(findings, (list, tuple))
        or len(scope) > MAX_RECORDS or len(findings) > MAX_RECORDS
    ):
        raise VerificationError("review scope and findings must be arrays", "plan_invalid")
    return reserve_job(folder, "review", {
        "unit": unit, "snapshot": snapshot, "purpose": purpose,
        "scope": ordered(scope), "findings": ordered(findings),
    }, owner_pid=owner_pid, unit=unit, request_identity=snapshot)


def verification_job_key(unit):
    return digest({"purpose": "verify", "identity": {"unit": unit}})


def active_verification(folder, unit):
    record = read_record(job_path(job_directory(folder), verification_job_key(unit)))
    if record and record.get("status") == "running" and owner_alive(record):
        return record
    return None


def active_review_jobs(folder, unit):
    directory = job_directory(folder)
    seen = 0
    for path in directory.glob("*.json"):
        seen += 1
        if seen > MAX_RECORDS:
            raise VerificationError("execution job registry is full", "execution_registry_invalid")
        record = read_record(path)
        if (
            record and record.get("purpose") == "review" and record.get("unit") == unit
            and record.get("status") == "running" and owner_alive(record)
        ):
            yield record


@contextlib.contextmanager
def unit_execution_lock(folder, unit, *, owned_job=None):
    """Serialize a unit's verify and contract mutation without nested state locks."""
    directory = Path(folder).resolve() / ".locks" / "execution"
    lock = locking.FileLock(directory / f"{digest(unit)}.lock", "unit execution", timeout=0)
    try:
        lock.__enter__()
    except locking.LockBusy as error:
        record = active_verification(folder, unit)
        if record is None:
            pid = lock.read_pid()
            record = {"key": verification_job_key(unit), "pid": pid, "token": "pending",
                      "status": "running", "result": None}
        raise ExecutionBusy(record) from error
    try:
        verification = active_verification(folder, unit)
        if verification is not None:
            raise ExecutionBusy(verification)
        for record in active_review_jobs(folder, unit):
            if (
                owned_job is not None and record["key"] == owned_job.get("key")
                and record["token"] == owned_job.get("token") and record["pid"] == os.getpid()
                and process_owner_alive(record)
            ):
                continue
            raise ExecutionBusy(record)
        yield
    finally:
        lock.__exit__(None, None, None)
