"""Attach a native Hermes task to an existing, externally reserved workspace."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from contextlib import contextmanager

from chef.guards import mission
from chef.infrastructure.configuration import locate, read_adapter
from chef.infrastructure.errors import UsageError
from chef.infrastructure.locking import FileLock, LockBusy, thread_lock_of
from chef.infrastructure.paths import inside_real_root, same_path
from chef.infrastructure.storage import atomic_write_json
from chef.runtimes.hermes.home import active_home, process_home
from chef.slots.contracts import NAME_PATTERN


def configured(adapter):
    return "externalSlots" in adapter or "externalSlots" in ((adapter.get("runtimes") or {}).get("hermes") or {})


@contextmanager
def workspace_transition(cwd):
    """Serialize admission in a shared gateway independently of profile state paths."""
    identity = os.stat(cwd)
    lock = thread_lock_of(f"hermes-external-{identity.st_dev}-{identity.st_ino}")
    if not lock.acquire(blocking=False):
        raise LockBusy("external workspace mission transition is in progress")
    try:
        yield
    finally:
        lock.release()


def token(value):
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def absolute_directory(value):
    if not token(value) or not os.path.isabs(value):
        raise UsageError("external slot directories must be absolute paths")
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise UsageError("external slot directory must exist and cannot be a link")
    return str(path.resolve(strict=True))


def state_path(root, adapter):
    value = (adapter.get("paths") or {}).get("stateDir")
    if not token(value) or os.path.isabs(value) or ".." in Path(value).parts:
        raise UsageError("external slot stateDir must be relative to the workspace")
    target = os.path.join(root, value)
    if not inside_real_root(root, target) or same_path(root, target):
        raise UsageError("external slot stateDir escaped the workspace")
    # Reject in-workspace links too. The coordinator owns the workspace-relative state.
    if os.path.normcase(os.path.realpath(target)) != os.path.normcase(os.path.normpath(target)):
        raise UsageError("external slot stateDir cannot use a symlink")
    return os.path.normpath(target)


class Slot:
    def __init__(self, located, adapter, name, *, gateway_home=None):
        settings = adapter.get("externalSlots")
        if not isinstance(settings, dict):
            raise UsageError("externalSlots must be an object")
        if adapter.get("slots", {}).get("enabled"):
            raise UsageError("external slots cannot enable managed slot operations")
        workspaces = settings.get("workspaces")
        if not isinstance(workspaces, dict) or name not in workspaces:
            raise UsageError("unknown external slot name")
        root = absolute_directory(settings.get("root"))
        self.main = absolute_directory(located.project)
        self.name = name
        self.adapter = located.adapter_path
        resolved = []
        for logical, workspace in workspaces.items():
            if not NAME_PATTERN.fullmatch(logical) or not isinstance(workspace, dict):
                raise UsageError("invalid external slot name or mapping")
            path = absolute_directory(workspace.get("path"))
            if (not inside_real_root(root, path) or any(
                    same_path(other, path) or inside_real_root(other, path) or inside_real_root(path, other)
                    for other in [self.main, *resolved])):
                raise UsageError("external slot path overlaps another workspace or escapes the configured root")
            resolved.append(path)
            if logical == name:
                self.cwd = path
                self.data = workspace.get("data", {})
        self.argv = settings.get("provider")
        protected = settings.get("protectedCommands", [])
        if not isinstance(protected, list) or not all(token(value) for value in protected):
            raise UsageError("external slot protectedCommands must be a list of regular expressions")
        self.protected = [re.compile(pattern, re.IGNORECASE) for pattern in protected]
        if (not isinstance(self.data, dict) or not isinstance(self.argv, list) or not self.argv
                or not all(token(value) for value in self.argv) or not os.path.isabs(self.argv[0])):
            raise UsageError("external slot requires an absolute provider command and object data")
        self.state = state_path(self.cwd, adapter)
        material = [self.main, self.cwd, self.name, self.state, self.argv, self.data, protected]
        self.fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        self.validate_adapter()
        digest = hashlib.sha256(os.fsencode(os.path.normcase(self.cwd))).hexdigest()
        journal_path = Path("chef", "external-slots", digest + ".json")
        self.profile_journal = active_home().resolve() / journal_path
        self.journal = self.profile_journal
        legacy = (Path(gateway_home) if gateway_home else process_home()).resolve() / journal_path
        if legacy != self.journal and os.path.lexists(legacy):
            if os.path.lexists(self.journal):
                raise UsageError("external slot has reservations in both Hermes homes; inspect both before continuing")
            # Existing reservations keep their original journal and lock until released.
            self.journal = legacy
        root = self.journal.parent
        if inside_real_root(self.cwd, root) or same_path(self.cwd, root):
            raise UsageError("external reservation state must be outside the worktree")

    def validate_adapter(self):
        located = locate(self.cwd, "hermes")
        if not same_path(located.project, self.cwd) or not same_path(located.adapter_path, self.adapter):
            raise UsageError("external slot picked a different project or adapter")

    def lock(self):
        return FileLock(str(self.journal.with_suffix(".lock")), "external slot")

    def check_command(self, command):
        if any(pattern.search(command) for pattern in self.protected):
            raise UsageError("external reservation commands belong to the mission guard")

    def call(self, action, record=None, clean=False):
        self.validate_adapter()
        payload = {"schema": 1, "action": action, "project": self.main, "workspace": self.cwd,
                   "name": self.name, "stateDir": self.state, "data": self.data, "clean": clean}
        if record:
            payload.update(who=record["who"], key=record["key"])
        try:
            result = subprocess.run(self.argv, cwd=self.main, input=json.dumps(payload).encode(),
                                    capture_output=True, timeout=5)
            response = json.loads(result.stdout)
            if result.returncode == 0 and response == {"ok": True}:
                return
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        # The provider's output and arguments can contain reservation identifiers.
        raise UsageError(f"external slot provider did not confirm {action}")

    def record(self):
        try:
            with self.journal.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        if (not isinstance(data, dict) or data.get("workspace") != self.cwd
                or data.get("project") != self.main or data.get("name") != self.name
                or data.get("fingerprint") != self.fingerprint
                or not all(token(data.get(field)) for field in ("who", "key", "mission", "phase"))):
            raise UsageError("external slot has an invalid reservation record")
        return data

    def write(self, record, phase):
        record["phase"] = phase
        atomic_write_json(self.journal, record, create_parents=True, durable=True)

    def check(self, session, mission_id=None, clean=False):
        record = self.record()
        active = mission.read_active(self.state)
        if (not record or record["phase"] != "active" or record["who"] != session or active.corrupt
                or active.session != session or active.runtime != "hermes" or active.id != record["mission"]
                or (mission_id is not None and mission_id != active.id)):
            raise UsageError("external slot reservation and ACTIVE are not owned by this session")
        self.call("check", record, clean)
        return record

    def available(self):
        active = mission.read_active(self.state)
        if active.corrupt or active.id or self.record():
            raise UsageError("external slot has an ACTIVE mission or unresolved reservation")
        self.call("status", clean=True)

    def transition(self, marker, session, request_id=None):
        action, mission_id = marker.group(1).lower(), marker.group(2)
        if not mission.valid_mission_id(mission_id):
            raise UsageError("invalid mission ID")
        outcome = mission.Outcome()
        with self.lock():
            if action == "open":
                if self.journal != self.profile_journal:
                    raise UsageError("an existing gateway reservation must be resolved before opening a new mission")
                self.available()
                if not token(request_id):
                    raise UsageError("external slot requires the trusted originating request ID")
                record = {"project": self.main, "workspace": self.cwd, "name": self.name,
                          "who": session, "key": request_id, "mission": mission_id,
                          "fingerprint": self.fingerprint}
                self.write(record, "opening")
                self.call("acquire", record, clean=True)
                try:
                    self.call("check", record, clean=True)
                except UsageError:
                    active = mission.read_active(self.state)
                    if not active.corrupt and not active.id:
                        self.call("release", record, clean=True)
                        self.journal.unlink()
                    raise
            else:
                record = self.check(session, mission_id, clean=True)
                if action == "close":
                    self.write(record, "closing")
                    self.call("release", record, clean=True)
            mission.handle_marker(marker, self.state, session, outcome, runtime="hermes")
            active = mission.read_active(self.state)
            if outcome.deny or active.corrupt:
                raise UsageError("external slot mission transition requires inspection")
            if action == "close":
                if active.id or os.path.lexists(mission.active_path_of(self.state)):
                    raise UsageError("external release succeeded but ACTIVE could not be closed")
                self.journal.unlink()
            elif action == "open":
                if (active.id, active.session, active.runtime) != (mission_id, session, "hermes"):
                    raise UsageError("external reservation acquired but ACTIVE was not recorded")
                self.write(record, "active")


def from_context(context, *, gateway_home=None):
    located = locate(context["project"], "hermes")
    slot = Slot(located, read_adapter(located.adapter_path, "hermes"), context["slot"], gateway_home=gateway_home)
    if (not same_path(slot.cwd, context["cwd"]) or not same_path(slot.adapter, context["adapter"])
            or slot.fingerprint != context["fingerprint"]):
        raise UsageError("external slot configuration changed after selection")
    return slot


def selection(located, adapter, name, session, resume, dry_run):
    slot = Slot(located, adapter, name)
    active = mission.read_active(slot.state)
    if resume:
        slot.check(session, clean=True)
    else:
        slot.available()
    return {"runtime": "hermes", "cwd": slot.cwd, "project": slot.main, "slot": name, "state": slot.state,
            "session_id": session, "mission": active.id, "mode": "resume" if resume else "new",
            "beforeRun": [], "dry_run": dry_run, "external": True, "adapter": slot.adapter,
            "fingerprint": slot.fingerprint}
