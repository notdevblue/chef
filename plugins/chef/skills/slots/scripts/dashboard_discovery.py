import fnmatch
import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path

import slots
import mission_guard

LOCATE_LOCK = threading.Lock()


class Config:
    def __init__(self, located, adapter, runtime="claude"):
        self.located = located
        self.runtime = runtime
        self.project_name = str(adapter.get("project") or os.path.basename(located.project))
        self.main = slots.normalize((adapter.get("vars") or {}).get("main") or located.project)
        self.state_dir = mission_guard.state_dir_of(self.main, adapter)
        self.stages = [stage for stage in adapter.get("stages") or [] if isinstance(stage, dict) and stage.get("id")]
        self.stale_hours = mission_guard.stale_hours_of(adapter)
        settings = adapter.get("slots") or {}
        self.names = []
        self.root = ""
        if settings.get("enabled"):
            self.root = settings.get("root") or ""
            self.names = list(dict.fromkeys(settings.get("names") or []))
            if not isinstance(self.root, str) or not self.root:
                raise slots.UsageError("slots.root 가 없음 (local.json 확인)")
            if any(not isinstance(name, str) or not slots.NAME_PATTERN.fullmatch(name) for name in self.names):
                raise slots.UsageError("slots.names 형식 오류")
        self.adapter = adapter
        self.rows_path = os.path.join(self.state_dir, "slots.tsv")

    def slot_path(self, name):
        return slots.normalize(os.path.join(self.root, name))

    def slot_state_dir(self, name):
        return mission_guard.state_dir_of(self.slot_path(name), self.adapter)


@dataclass
class Workspace:
    label: str
    root: str
    state_dir: str
    stages: list
    stale_hours: float
    row: dict = field(default_factory=dict)
    key: str = ""


def canonical(path):
    return os.path.normcase(slots.request_realpath(os.path.abspath(path)))


def mission_folder_allowed(state_dir, mission_id):
    expected = os.path.join(slots.request_realpath(state_dir), mission_id)
    real = slots.request_realpath(os.path.join(state_dir, mission_id))
    return mission_guard.valid_mission_id(mission_id) and os.path.normcase(real) == os.path.normcase(expected)


def mission_metadata_allowed(state_dir, mission_id):
    if not mission_folder_allowed(state_dir, mission_id):
        return False
    folder = os.path.join(state_dir, mission_id)
    metadata = os.path.join(folder, "mission.md")
    expected = os.path.join(slots.request_realpath(folder), "mission.md")
    return os.path.normcase(slots.request_realpath(metadata)) == os.path.normcase(expected) and os.path.isfile(metadata)


def mission_ids(state_dir):
    found = []
    with os.scandir(state_dir) as entries:
        for entry in entries:
            if entry.is_dir() and mission_metadata_allowed(state_dir, entry.name):
                found.append(entry.name)
    return found


def expand_root(root, problems):
    path = Path(os.path.abspath(root))
    candidates = [path.anchor]
    for part in path.parts[1:]:
        expanded = []
        for parent in candidates:
            if any(char in part for char in "*?["):
                try:
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            if not fnmatch.fnmatch(os.path.normcase(entry.name), os.path.normcase(part)):
                                continue
                            try:
                                if entry.is_dir():
                                    expanded.append(entry.path)
                            except FileNotFoundError:
                                continue
                            except OSError as error:
                                problems.append(f"등록 경로 읽기 실패: {entry.path}: {error}")
                except FileNotFoundError:
                    continue
                except OSError as error:
                    problems.append(f"등록 경로 읽기 실패: {parent}: {error}")
            else:
                child = os.path.join(parent, part)
                try:
                    if stat.S_ISDIR(os.stat(child).st_mode):
                        expanded.append(child)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    problems.append(f"등록 경로 읽기 실패: {child}: {error}")
        candidates = sorted(expanded)
    return candidates


def workspace_config(root, runtime, problems, fallback=""):
    with LOCATE_LOCK:
        saved = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            located = mission_guard.locate_adapter({"cwd": os.path.abspath(root), "han_runtime": runtime})
        except mission_guard.GuardOff as error:
            if not fallback:
                raise
            problems.append(f"레지스트리: {error}")
            located = None
        finally:
            if saved is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = saved
    adapter_path = fallback
    if located:
        adapter_path = located.adapter_path
    if not adapter_path:
        raise slots.UsageError(f"어댑터를 찾지 못함: {root}")
    adapter = slots.read_adapter(adapter_path, runtime)
    return Config(mission_guard.Located(root, adapter_path), adapter, runtime)


def selected_workspace(workspace, runtime, problems, fallback):
    selected = workspace_config(workspace.root, runtime, problems, fallback)
    workspace.state_dir = mission_guard.state_dir_of(workspace.root, selected.adapter)
    workspace.stages = selected.stages
    workspace.stale_hours = selected.stale_hours
    return workspace


def configured_workspaces(config, problems):
    result = [Workspace("main", config.main, config.state_dir, config.stages, config.stale_hours)]
    if config.root:
        try:
            rows = slots.Rows(config.rows_path).read()
        except FileNotFoundError:
            rows = []
        except (OSError, ValueError, slots.UsageError) as error:
            problems.append(f"slots.tsv: {error}")
            rows = []
        table = {row["name"]: row for row in rows if slots.NAME_PATTERN.fullmatch(row["name"])}
        for name in sorted({*config.names, *table}, key=lambda value: [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]):
            result.append(Workspace(name, config.slot_path(name), config.slot_state_dir(name), config.stages, config.stale_hours, table.get(name, {})))
    return result


def discover(config):
    problems = []
    runtime = config.runtime
    current = config
    if hasattr(config, "located"):
        current = Config(config.located, slots.read_adapter(config.located.adapter_path, runtime), runtime)
    candidates = configured_workspaces(current, problems)
    if hasattr(current, "located"):
        selected = []
        for workspace in candidates:
            try:
                selected.append(selected_workspace(workspace, runtime, problems, current.located.adapter_path))
            except (slots.UsageError, mission_guard.GuardOff, OSError, ValueError, TypeError, AttributeError) as error:
                problems.append(f"{workspace.root}: {error}")
        candidates = selected
    legacy_count = len(candidates)
    if hasattr(current, "located"):
        root = current.located.project
        candidates.append(Workspace(os.path.basename(root), root, mission_guard.state_dir_of(root, current.adapter), current.stages, current.stale_hours))
    trusted_count = len(candidates)
    try:
        registry = mission_guard.resolve_registry(runtime)
        if registry.path:
            if not stat.S_ISDIR(os.stat(registry.path).st_mode):
                raise slots.UsageError(f"레지스트리 폴더 아님: {registry.path}")
            try:
                os.stat(os.path.join(registry.path, mission_guard.REGISTRY_FILE))
            except FileNotFoundError:
                problems.append(f"{mission_guard.REGISTRY_FILE} 없음: {registry.path}")
            entries = mission_guard.load_registry(registry.path)
            for entry in entries:
                adapter_path = os.path.join(registry.path, entry["adapter"], "adapter.json")
                try:
                    slots.read_adapter(adapter_path, runtime)
                except (slots.UsageError, mission_guard.GuardOff, OSError, ValueError, TypeError, AttributeError) as error:
                    problems.append(f"{adapter_path}: {error}")
                try:
                    for pattern in entry["roots"]:
                        try:
                            roots = expand_root(pattern, problems)
                        except OSError as error:
                            problems.append(f"등록 경로 읽기 실패: {pattern}: {error}")
                            continue
                        if not roots:
                            problems.append(f"등록 경로 없음: {pattern}")
                        for root in roots:
                            try:
                                project = workspace_config(root, runtime, problems)
                                additional = configured_workspaces(project, problems)
                                additional.append(Workspace(os.path.basename(root), root, mission_guard.state_dir_of(root, project.adapter), project.stages, project.stale_hours))
                                for workspace in additional:
                                    try:
                                        candidates.append(selected_workspace(workspace, runtime, problems, project.located.adapter_path))
                                    except (slots.UsageError, mission_guard.GuardOff, OSError, ValueError, TypeError, AttributeError) as error:
                                        problems.append(f"{workspace.root}: {error}")
                            except (slots.UsageError, mission_guard.GuardOff, OSError, ValueError, TypeError, AttributeError) as error:
                                problems.append(f"{root}: {error}")
                except (slots.UsageError, mission_guard.GuardOff, OSError, ValueError, TypeError, AttributeError) as error:
                    problems.append(f"{adapter_path}: {error}")
    except (slots.UsageError, mission_guard.GuardOff, OSError, ValueError) as error:
        problems.append(f"레지스트리: {error}")
    found, identities, keys = [], set(), set()
    for index, workspace in enumerate(candidates):
        identity = (canonical(workspace.root), canonical(workspace.state_dir))
        if identity in identities:
            continue
        if index >= trusted_count:
            try:
                if not mission_ids(workspace.state_dir):
                    continue
            except FileNotFoundError:
                continue
            except OSError as error:
                problems.append(f"미션 폴더 읽기 실패: {workspace.state_dir}: {error}")
                continue
        identities.add(identity)
        key = workspace.label
        if index >= legacy_count or key in keys or not slots.NAME_PATTERN.fullmatch(key):
            key = "workspace-" + hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
        workspace.key = key
        keys.add(key)
        found.append(workspace)
    return found, problems
