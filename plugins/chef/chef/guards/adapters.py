"""guards / adapters."""

import fnmatch
import glob
import json
import ntpath
import os
import sys
from typing import NamedTuple

from chef.guards.contracts import (
    ADAPTER_RELATIVE,
    LOCAL_FILE,
    MISSION_ID,
    REGISTRY_ENV,
    REGISTRY_FILE,
    REPO_ENV,
    SUPPORTED_SCHEMA,
    USER_CONFIG_ENV,
)
from chef.guards.logging import append_log
from chef.infrastructure.paths import inside_real_root, normalize_path, same_path
from chef.locations import PLUGIN_ROOT


class GuardOff(Exception):
    pass


class AdapterNotFound(Exception):
    pass


class Located(NamedTuple):
    project: str
    adapter_path: str


class Registry(NamedTuple):
    path: str
    source: str


def read_json_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def valid_mission_id(mission_id):
    return bool(MISSION_ID.fullmatch(mission_id)) and ".." not in mission_id


def in_place_adapters_dir():
    # hooks -> plugins/chef -> plugins -> 저장소 루트. 설치된 캐시 폴더에선 이 경로가 없음
    here = str(PLUGIN_ROOT / "hooks")
    return os.path.normpath(os.path.join(here, "..", "..", "..", "adapters"))


def han_repo_adapters_dir():
    repo = os.environ.get(REPO_ENV, "").strip()
    if not repo:
        return ""
    path = os.path.abspath(os.path.join(repo, "adapters"))
    if not os.path.isdir(path):
        raise GuardOff(f"HAN_REPO/adapters 없음: {path}")
    return path


def adapter_relatives(runtime):
    if runtime == "hermes":
        return (
            (".hermes", "orchestrator", "adapter.json"),
            ADAPTER_RELATIVE,
            (".codex", "orchestrator", "adapter.json"),
            (".grok", "orchestrator", "adapter.json"),
        )
    if runtime == "codex":
        return ((".codex", "orchestrator", "adapter.json"), ADAPTER_RELATIVE)
    if runtime == "grok":
        # 가까운 폴더가 우선이고, 한 폴더 안에서는 .grok → .claude → .codex
        return (
            (".grok", "orchestrator", "adapter.json"),
            ADAPTER_RELATIVE,
            (".codex", "orchestrator", "adapter.json"),
        )
    return (ADAPTER_RELATIVE,)


def registry_missing_message(runtime, *, project=None, tried=()):
    parts = list(tried)
    if runtime == "hermes":
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        parts.append(f"HERMES_HOME/chef/adapters={os.path.abspath(os.path.join(home, 'chef', 'adapters'))}")
    elif runtime == "codex":
        parts.append("Codex config.toml shell_environment_policy.set.HAN_ADAPTERS_DIR")
    elif runtime == "grok":
        parts.append("HAN_REPO/adapters")
    else:
        parts.append(f"{USER_CONFIG_ENV} (Claude adapters_dir)")
    env_value = os.environ.get(REGISTRY_ENV, "")
    parts.insert(0, f"{REGISTRY_ENV}={env_value or 'unset'}")
    inplace = in_place_adapters_dir()
    if os.path.isdir(inplace) and not project_may_use_in_place(project):
        parts.append(f"in-place={inplace} (건너뜀: 이 프로젝트는 chef 저장소가 아님)")
    elif os.path.isdir(inplace):
        parts.append(f"in-place={inplace}")
    return "레지스트리 경로 미설정 (" + "; ".join(parts) + ")"


def codex_config_registry_dir():
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    path = os.path.join(codex_home, "config.toml")
    try:
        os.stat(path)
    except FileNotFoundError:
        return ""
    except OSError as e:
        raise GuardOff(f"Codex config.toml 접근 실패 ({path}: {e})")
    try:
        import tomllib
    except ImportError:
        raise GuardOff(f"Codex config.toml 을 읽으려면 Python 3.11 이상이 필요함 ({path})")
    try:
        with open(path, "rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as e:
        raise GuardOff(f"Codex config.toml 읽기 실패 ({path}: {e})")
    policy = config.get("shell_environment_policy", {})
    if not isinstance(policy, dict):
        raise GuardOff(f"Codex config.toml 형식 오류 ({path}): shell_environment_policy 는 객체여야 함")
    values = policy.get("set", {})
    if not isinstance(values, dict):
        raise GuardOff(f"Codex config.toml 형식 오류 ({path}): shell_environment_policy.set 는 객체여야 함")
    configured = values.get(REGISTRY_ENV, "")
    if configured == "":
        return ""
    if not isinstance(configured, str):
        raise GuardOff(f"Codex config.toml 형식 오류 ({path}): {REGISTRY_ENV} 는 문자열이어야 함")
    return configured


def in_place_repo_root():
    return os.path.dirname(in_place_adapters_dir())


def project_may_use_in_place(project=None):
    """The in-tree adapters/ example is only for the chef repository itself."""
    folder = in_place_adapters_dir()
    if not os.path.isdir(folder):
        return False
    root = in_place_repo_root()
    start = os.path.abspath(project or os.getcwd())
    return same_path(start, root) or inside_real_root(root, start)


def resolve_registry(runtime="claude", *, project=None):
    """Single registry-path resolver for CLI, plugins, chef_slot, and mission entry.

    chef-studio and any other launcher must call this (not a private copy) so
    start and later sessions agree on the path. The in-tree example adapters/
    folder is never a silent fallback for a real project.
    """
    sources = [("env", REGISTRY_ENV)]
    if runtime == "claude":
        sources.append(("userConfig", USER_CONFIG_ENV))
    for source, variable in sources:
        configured = os.environ.get(variable)
        if configured:
            return Registry(os.path.abspath(configured), source)
    if runtime == "codex":
        configured = codex_config_registry_dir()
        if configured:
            return Registry(os.path.abspath(configured), "codexConfig")
    if runtime == "grok":
        configured = han_repo_adapters_dir()
        if configured:
            return Registry(configured, "hanRepo")
    if runtime == "hermes":
        home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        configured = os.path.join(home, "chef", "adapters")
        return Registry(os.path.abspath(configured), "hermesHome")
    if project_may_use_in_place(project):
        return Registry(os.path.abspath(in_place_adapters_dir()), "in-place")
    return Registry("", "none")


def registry_dir(runtime="claude", *, project=None):
    return resolve_registry(runtime, project=project).path


def registry_adapter_path(name, runtime="claude", *, project=None):
    return os.path.join(registry_dir(runtime, project=project), name, "adapter.json")


def root_matches(folder, root):
    if ntpath.splitdrive(root)[0]:
        folder = ntpath.normcase(ntpath.normpath(folder)).replace("\\", "/").rstrip("/")
        root = ntpath.normcase(ntpath.normpath(root)).replace("\\", "/").rstrip("/")
    elif not glob.has_magic(root):
        return same_path(folder, root)
    folder_parts = normalize_path(folder).split("/")
    root_parts = normalize_path(root).split("/")
    if len(folder_parts) == len(root_parts) and all(
        fnmatch.fnmatchcase(part, pattern) for part, pattern in zip(folder_parts, root_parts)
    ):
        return True
    # Resolve actual matched directories so native aliases and filesystem case
    # behavior work without applying Windows case folding to POSIX paths.
    return any(same_path(folder, candidate) for candidate in glob.iglob(root, include_hidden=True))


def load_registry(directory):
    path = os.path.join(directory, REGISTRY_FILE)
    if not os.path.isfile(path):
        return []
    try:
        entries = read_json_file(path)
    except (OSError, ValueError) as e:
        raise GuardOff(f"어댑터 registry 읽기 실패 ({path}: {e})")
    valid = isinstance(entries, list)
    for entry in entries if valid else []:
        valid = (
            valid
            and isinstance(entry, dict)
            and isinstance(entry.get("adapter"), str)
            and valid_mission_id(entry["adapter"])
            and isinstance(entry.get("roots"), list)
            and all(isinstance(root, str) for root in entry["roots"])
        )
    if not valid:
        raise GuardOff(f"어댑터 registry 형식 오류 ({path}): [{{adapter, roots[]}}] 배열이어야 함")
    return entries


def ancestors(start):
    folders = []
    folder = os.path.abspath(start)
    while True:
        folders.append(folder)
        parent = os.path.dirname(folder)
        if parent == folder:
            return folders
        folder = parent


def find_adapter(payload):
    from chef.guards.slot_binding import discover_slot_binding, entry_matches

    runtime = payload.get("han_runtime", "claude")
    start = (os.environ.get("CLAUDE_PROJECT_DIR") if runtime == "claude" else "") or payload.get("cwd") or ""
    if not start:
        raise AdapterNotFound("시작 폴더 없음 (CLAUDE_PROJECT_DIR·cwd 가 비어 있음)")
    chain = ancestors(start)
    for folder in chain:
        for relative in adapter_relatives(runtime):
            path = os.path.join(folder, *relative)
            if os.path.isfile(path):
                return Located(folder, path)
    bound_folder, binding = discover_slot_binding(start)
    registries = []
    if binding:
        bound_registry = Registry(os.path.abspath(binding["registry"]), "slotBinding")
        registries.append(bound_registry)
        adapter_path = binding.get("adapterPath") or ""
        if not adapter_path and binding.get("adapter"):
            adapter_path = os.path.join(bound_registry.path, binding["adapter"], "adapter.json")
        if adapter_path and os.path.isfile(adapter_path):
            append_log({"registry_source": "slotBinding", "registry_dir": bound_registry.path,
                        "adapter_path": os.path.abspath(adapter_path)})
            return Located(bound_folder, os.path.abspath(adapter_path))
    env_registry = resolve_registry(runtime, project=start)
    log = {"registry_source": env_registry.source, "registry_dir": env_registry.path}
    if binding:
        log["binding_registry"] = binding["registry"]
    append_log(log)
    if env_registry.path and all(os.path.abspath(env_registry.path) != item.path for item in registries):
        registries.append(env_registry)
    if not registries or not any(item.path for item in registries):
        raise AdapterNotFound(registry_missing_message(runtime, project=start))
    tried = []
    slot_name = os.path.basename(os.path.abspath(start).rstrip(os.sep))
    for registry in registries:
        directory = registry.path
        registry_file = os.path.join(directory, REGISTRY_FILE)
        if not os.path.isdir(directory):
            tried.append(f"레지스트리 폴더 없음: {directory} (출처 {registry.source})")
            continue
        if not os.path.isfile(registry_file):
            tried.append(f"{REGISTRY_FILE} 없음: {registry_file} (출처 {registry.source})")
            continue
        entries = load_registry(directory)
        allow_basename = registry.source == "slotBinding"
        for folder in chain:
            name = os.path.basename(folder.rstrip(os.sep)) or slot_name
            for entry in entries:
                if entry_matches(folder, entry, slot_name=name, allow_basename=allow_basename):
                    return Located(folder, os.path.join(directory, entry["adapter"], "adapter.json"))
        tried.append(f"레지스트리 roots/identities 에 {start} 가 맞는 항목 없음 ({registry_file}, 출처 {registry.source})")
    raise AdapterNotFound("; ".join(tried))


def locate_adapter(payload):
    try:
        return find_adapter(payload)
    except AdapterNotFound:
        return None


def merge_local(base, local):
    merged = dict(base)
    for key, value in local.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_local(merged[key], value)
        else:
            merged[key] = value
    return merged


def read_local(adapter_path, problems):
    path = os.path.join(os.path.dirname(adapter_path), LOCAL_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        local = read_json_file(path)
    except (OSError, ValueError) as e:
        problems.append(f"local.json 무시 (adapter.json 만 씀): {path}: {e}")
        return {}
    if not isinstance(local, dict):
        problems.append(f"local.json 무시 (adapter.json 만 씀): {path}: 객체가 아님")
        return {}
    return local


def runtime_overlay(adapter, runtime, source):
    runtimes = adapter.get("runtimes", {})
    if not isinstance(runtimes, dict):
        raise GuardOff(f"{source} 의 runtimes 는 객체여야 함")
    overlay = runtimes.get(runtime, {})
    if not isinstance(overlay, dict):
        raise GuardOff(f"{source} 의 runtimes.{runtime} 는 객체여야 함")
    return overlay


def platform_name():
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"


def platform_overlay(adapter, source):
    platforms = adapter.get("platforms", {})
    if not isinstance(platforms, dict):
        raise GuardOff(f"{source} 의 platforms 는 객체여야 함")
    unknown = set(platforms) - {"windows", "macos", "linux"}
    if unknown:
        raise GuardOff(f"{source} 의 platforms 에 지원하지 않는 OS 이름이 있음")
    for name, value in platforms.items():
        if not isinstance(value, dict):
            raise GuardOff(f"{source} 의 platforms.{name} 는 객체여야 함")
    return platforms.get(platform_name(), {})


def file_overlay(adapter, runtime, source):
    """Apply one file's platform and runtime specificity before local overrides."""
    base = merge_local(adapter, platform_overlay(adapter, source))
    selected = runtime_overlay(base, runtime, source)
    return merge_local(merge_local(base, selected), platform_overlay(selected, source + ".runtimes." + runtime))


def load_adapter(adapter_path, problems=None, runtime="claude"):
    if problems is None:
        problems = []
    try:
        adapter = read_json_file(adapter_path)
    except (OSError, ValueError) as e:
        raise GuardOff(f"adapter.json 읽기 실패 ({adapter_path}: {e})")
    if not isinstance(adapter, dict):
        raise GuardOff(f"adapter.json schema 가 1이 아님 ({adapter_path})")
    local = read_local(adapter_path, problems)
    adapter = merge_local(file_overlay(adapter, runtime, "adapter.json"), file_overlay(local, runtime, "local.json"))
    if adapter.get("schema") != SUPPORTED_SCHEMA:
        raise GuardOff(f"adapter.json schema 가 1이 아님 ({adapter_path})")
    return adapter


def state_dir_of(project, adapter):
    relative = (adapter.get("paths") or {}).get("stateDir")
    if not isinstance(relative, str) or not relative:
        raise GuardOff("adapter.json 에 paths.stateDir 없음")
    return os.path.normpath(os.path.join(project, relative))
