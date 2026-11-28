import fnmatch
import hashlib
import os
import re
import stat
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from chef.guards import adapters as adapter_guard
from chef.guards import mission as mission_guard
from chef.guards import slot_binding
from chef.infrastructure import configuration as infrastructure_configuration
from chef.infrastructure import errors as infrastructure_errors
from chef.infrastructure import paths as infrastructure_paths
from chef.locations import PLUGIN_ROOT
from chef.slots import contracts as slots_contracts
from chef.slots import storage as slots_storage

LOCATE_LOCK = threading.Lock()
DISCOVERY_INPUTS = threading.local()


def watch(path, *, identity=False):
    inputs = getattr(DISCOVERY_INPUTS, "current", None)
    if inputs is not None:
        inputs.add(path, identity=identity)


@contextmanager
def watching(inputs):
    previous = getattr(DISCOVERY_INPUTS, "current", None)
    DISCOVERY_INPUTS.current = inputs
    try:
        yield
    finally:
        DISCOVERY_INPUTS.current = previous


def watch_adapter(path):
    watch(path)
    watch(os.path.join(os.path.dirname(path), "local.json"))


class Config:
    def __init__(self, located, adapter, runtime="claude"):
        self.located = located
        self.runtime = runtime
        self.project_name = str(adapter.get("project") or os.path.basename(located.project))
        self.main = infrastructure_paths.normalize((adapter.get("vars") or {}).get("main") or located.project)
        self.state_dir = mission_guard.state_dir_of(self.main, adapter)
        self.stages = [
            stage for stage in adapter.get("stages") or [] if isinstance(stage, dict) and stage.get("id")
        ]
        self.stale_hours = mission_guard.stale_hours_of(adapter)
        settings = adapter.get("slots") or {}
        self.names = []
        self.root = ""
        if settings.get("enabled"):
            self.root = settings.get("root") or ""
            self.names = list(dict.fromkeys(settings.get("names") or []))
            if not isinstance(self.root, str) or not self.root:
                raise infrastructure_errors.UsageError("slots.root 가 없음 (local.json 확인)")
            if any(
                not isinstance(name, str) or not slots_contracts.NAME_PATTERN.fullmatch(name)
                for name in self.names
            ):
                raise infrastructure_errors.UsageError("slots.names 형식 오류")
        self.adapter = adapter
        self.rows_path = os.path.join(self.state_dir, "slots.tsv")

    def slot_path(self, name):
        return infrastructure_paths.normalize(os.path.join(self.root, name))

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
    requires_mission: bool = False


def canonical(path):
    return os.path.normcase(infrastructure_paths.request_realpath(os.path.abspath(path)))


def mission_folder_allowed(state_dir, mission_id):
    expected = os.path.join(infrastructure_paths.request_realpath(state_dir), mission_id)
    real = infrastructure_paths.request_realpath(os.path.join(state_dir, mission_id))
    return mission_guard.valid_mission_id(mission_id) and os.path.normcase(real) == os.path.normcase(expected)


def mission_metadata_allowed(state_dir, mission_id):
    if not mission_folder_allowed(state_dir, mission_id):
        return False
    folder = os.path.join(state_dir, mission_id)
    metadata = os.path.join(folder, "mission.md")
    expected = os.path.join(infrastructure_paths.request_realpath(folder), "mission.md")
    return os.path.normcase(infrastructure_paths.request_realpath(metadata)) == os.path.normcase(
        expected
    ) and os.path.isfile(metadata)


def iter_mission_ids(state_dir):
    with os.scandir(state_dir) as entries:
        for entry in entries:
            if entry.is_dir() and mission_metadata_allowed(state_dir, entry.name):
                yield entry.name


def mission_ids(state_dir):
    return list(iter_mission_ids(state_dir))


def has_mission(state_dir):
    try:
        with closing(iter_mission_ids(state_dir)) as missions:
            return next(missions, None) is not None
    except OSError:
        return False


def expand_root(root, problems):
    path = Path(os.path.abspath(root))
    candidates = [path.anchor]
    for part in path.parts[1:]:
        expanded = []
        for parent in candidates:
            if any(char in part for char in "*?["):
                watch(parent)
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
                watch(child, identity=True)
                try:
                    if stat.S_ISDIR(os.stat(child).st_mode):
                        expanded.append(child)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    problems.append(f"등록 경로 읽기 실패: {child}: {error}")
        candidates = sorted(expanded)
    return candidates


def locate_workspace(root, runtime):
    if getattr(DISCOVERY_INPUTS, "current", None) is not None:
        for ancestor in adapter_guard.ancestors(root):
            for relative in adapter_guard.adapter_relatives(runtime):
                watch_adapter(os.path.join(ancestor, *relative))
            watch(os.path.join(ancestor, ".p4config"))
            watch(slot_binding.binding_path(ancestor))
            binding = slot_binding.read_slot_binding(ancestor)
            if binding:
                watch(os.path.join(binding["registry"], mission_guard.REGISTRY_FILE))
    with LOCATE_LOCK:
        saved = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            return mission_guard.locate_adapter({"cwd": os.path.abspath(root), "han_runtime": runtime})
        finally:
            if saved is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = saved


def workspace_config(root, runtime, adapter_path, adapters):
    if not adapter_path:
        raise infrastructure_errors.UsageError(f"어댑터를 찾지 못함: {root}")
    if adapter_path not in adapters:
        watch_adapter(adapter_path)
        adapters[adapter_path] = infrastructure_configuration.read_adapter(adapter_path, runtime)
    return Config(mission_guard.Located(root, adapter_path), adapters[adapter_path], runtime)


def selected_workspace(workspace, resolve, fallback):
    selected = resolve(workspace.root, fallback)
    workspace.state_dir = mission_guard.state_dir_of(workspace.root, selected.adapter)
    workspace.stages = selected.stages
    workspace.stale_hours = selected.stale_hours
    return workspace


def configured_workspaces(config, problems):
    result = [Workspace("main", config.main, config.state_dir, config.stages, config.stale_hours)]
    if config.root:
        watch(config.rows_path)
        try:
            rows = slots_storage.Rows(config.rows_path).read()
        except FileNotFoundError:
            rows = []
        except (OSError, ValueError, infrastructure_errors.UsageError) as error:
            problems.append(f"slots.tsv: {error}")
            rows = []
        table = {row["name"]: row for row in rows if slots_contracts.NAME_PATTERN.fullmatch(row["name"])}
        for name in sorted(
            {*config.names, *table},
            key=lambda value: [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)],
        ):
            result.append(
                Workspace(
                    name,
                    config.slot_path(name),
                    config.slot_state_dir(name),
                    config.stages,
                    config.stale_hours,
                    table.get(name, {}),
                )
            )
    return result


def discover(config):
    problems = []
    runtime = config.runtime
    located_roots, resolved, adapters = {}, {}, {}
    expanded, slot_groups = set(), set()

    def resolve(root, fallback=""):
        root_key = os.path.normcase(os.path.abspath(root))
        if root_key not in located_roots:
            try:
                located_roots[root_key] = locate_workspace(root, runtime)
            except mission_guard.GuardOff as error:
                located_roots[root_key] = error
        located = located_roots[root_key]
        error = located if isinstance(located, mission_guard.GuardOff) else None
        if error and not fallback:
            raise error
        adapter_path = located.adapter_path if located is not None and error is None else fallback
        key = (root_key, adapter_path)
        if key not in resolved:
            if error:
                problems.append(f"레지스트리: {error}")
            resolved[key] = workspace_config(root, runtime, adapter_path, adapters)
        return resolved[key]

    current = config
    if hasattr(config, "located"):
        watch_adapter(config.located.adapter_path)
        current = Config(
            config.located,
            infrastructure_configuration.read_adapter(config.located.adapter_path, runtime),
            runtime,
        )
        slot_groups.add((current.located.adapter_path, current.main))
    candidates = configured_workspaces(current, problems)
    if hasattr(current, "located"):
        selected = []
        for workspace in candidates:
            try:
                selected.append(
                    selected_workspace(workspace, resolve, current.located.adapter_path)
                )
            except (
                infrastructure_errors.UsageError,
                mission_guard.GuardOff,
                OSError,
                ValueError,
                TypeError,
                AttributeError,
            ) as error:
                problems.append(f"{workspace.root}: {error}")
        candidates = selected
    legacy_count = len(candidates)
    if hasattr(current, "located"):
        root = current.located.project
        candidates.append(
            Workspace(
                os.path.basename(root),
                root,
                mission_guard.state_dir_of(root, current.adapter),
                current.stages,
                current.stale_hours,
            )
        )
    trusted_count = len(candidates)
    try:
        registry = mission_guard.resolve_registry(runtime)
        if registry.path:
            watch(os.path.join(registry.path, mission_guard.REGISTRY_FILE))
            if not stat.S_ISDIR(os.stat(registry.path).st_mode):
                raise infrastructure_errors.UsageError(f"레지스트리 폴더 아님: {registry.path}")
            try:
                os.stat(os.path.join(registry.path, mission_guard.REGISTRY_FILE))
            except FileNotFoundError:
                problems.append(f"{mission_guard.REGISTRY_FILE} 없음: {registry.path}")
            entries = mission_guard.load_registry(registry.path)
            for entry in entries:
                adapter_path = os.path.join(registry.path, entry["adapter"], "adapter.json")
                watch_adapter(adapter_path)
                try:
                    if adapter_path not in adapters:
                        adapters[adapter_path] = infrastructure_configuration.read_adapter(adapter_path, runtime)
                except (
                    infrastructure_errors.UsageError,
                    mission_guard.GuardOff,
                    OSError,
                    ValueError,
                    TypeError,
                    AttributeError,
                ) as error:
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
                            key = os.path.normcase(os.path.abspath(root))
                            if key in expanded:
                                continue
                            expanded.add(key)
                            try:
                                project = resolve(root)
                                group = (project.located.adapter_path, project.main)
                                additional = []
                                if group not in slot_groups:
                                    slot_groups.add(group)
                                    additional = configured_workspaces(project, problems)
                                additional.append(
                                    Workspace(
                                        os.path.basename(root),
                                        root,
                                        mission_guard.state_dir_of(root, project.adapter),
                                        project.stages,
                                        project.stale_hours,
                                    )
                                )
                                for workspace in additional:
                                    try:
                                        candidates.append(
                                            selected_workspace(
                                                workspace, resolve, project.located.adapter_path
                                            )
                                        )
                                    except (
                                        infrastructure_errors.UsageError,
                                        mission_guard.GuardOff,
                                        OSError,
                                        ValueError,
                                        TypeError,
                                        AttributeError,
                                    ) as error:
                                        problems.append(f"{workspace.root}: {error}")
                            except (
                                infrastructure_errors.UsageError,
                                mission_guard.GuardOff,
                                OSError,
                                ValueError,
                                TypeError,
                                AttributeError,
                            ) as error:
                                problems.append(f"{root}: {error}")
                except (
                    infrastructure_errors.UsageError,
                    mission_guard.GuardOff,
                    OSError,
                    ValueError,
                    TypeError,
                    AttributeError,
                ) as error:
                    problems.append(f"{adapter_path}: {error}")
    except (infrastructure_errors.UsageError, mission_guard.GuardOff, OSError, ValueError) as error:
        problems.append(f"레지스트리: {error}")
    found, identities, keys = [], set(), set()
    for index, workspace in enumerate(candidates):
        identity = (canonical(workspace.root), canonical(workspace.state_dir))
        if identity in identities:
            continue
        workspace.requires_mission = index >= trusted_count
        identities.add(identity)
        key = workspace.label
        if index >= legacy_count or key in keys or not slots_contracts.NAME_PATTERN.fullmatch(key):
            key = "workspace-" + hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
        workspace.key = key
        keys.add(key)
        found.append(workspace)
    return found, problems
