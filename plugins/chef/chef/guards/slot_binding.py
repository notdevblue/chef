"""Persist the adapter registry chosen at slot start so later processes can reuse it.

Slot create/start and Hermes chef_slot often run with different environments
(HAN_ADAPTERS_DIR vs $HERMES_HOME/chef/adapters). The binding file is the
slot's own record of the registry path and a stable project identity, so
those environments cannot silently diverge.
"""

from __future__ import annotations

import json
import os
import re

from chef.infrastructure.paths import canonical_path, same_path
from chef.infrastructure.storage import atomic_write_text

BINDING_RELATIVE = (".chef", "binding.json")
BINDING_SCHEMA = 1
PATH_KINDS = ("path", "canonicalPath", "p4ClientRoot")


def binding_path(slot):
    return os.path.join(slot, *BINDING_RELATIVE)


def read_p4_client(slot):
    try:
        with open(os.path.join(slot, ".p4config"), encoding="utf-8-sig") as handle:
            match = re.search(r"^P4CLIENT=(.*)$", handle.read(), re.M)
    except OSError:
        return ""
    return match.group(1).strip() if match else ""


def project_identities(path, *, p4_client="", p4_root="", project_id=""):
    """Stable ids for a project/slot folder: live paths plus optional Perforce identity."""
    identities = []
    seen = set()

    def add(kind, value):
        if not value:
            return
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        identities.append({"kind": kind, "value": value})

    if path:
        absolute = os.path.abspath(path)
        add("path", absolute)
        add("canonicalPath", canonical_path(absolute))
    client = p4_client or (read_p4_client(path) if path else "")
    add("p4Client", client)
    root = p4_root or (path if client else "")
    if root:
        add("p4ClientRoot", os.path.abspath(root))
        add("p4ClientRoot", canonical_path(root))
    add("projectId", project_id)
    return identities


def identities_match(folder, recorded):
    if not folder or not recorded:
        return False
    live = project_identities(folder)
    live_paths = [item["value"] for item in live if item["kind"] in PATH_KINDS]
    recorded_paths = [
        item.get("value") for item in recorded if isinstance(item, dict) and item.get("kind") in PATH_KINDS
    ]
    for left in live_paths:
        for right in recorded_paths:
            if right and (same_path(left, right) or left == right or canonical_path(left) == canonical_path(right)):
                return True
    live_clients = {item["value"] for item in live if item["kind"] == "p4Client" and item["value"]}
    recorded_clients = {
        item.get("value")
        for item in recorded
        if isinstance(item, dict) and item.get("kind") == "p4Client" and item.get("value")
    }
    return bool(live_clients and live_clients & recorded_clients)


def read_slot_binding(slot):
    path = binding_path(slot)
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != BINDING_SCHEMA:
        return {}
    registry = data.get("registry")
    if not isinstance(registry, str) or not registry:
        return {}
    return data


def discover_slot_binding(start):
    folder = os.path.abspath(start)
    while True:
        binding = read_slot_binding(folder)
        if binding:
            return folder, binding
        parent = os.path.dirname(folder)
        if parent == folder:
            return "", {}
        folder = parent


def write_slot_binding(slot, payload):
    body = dict(payload)
    body["schema"] = BINDING_SCHEMA
    path = binding_path(slot)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_text(path, json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    return path


def entry_matches(folder, entry, *, slot_name="", allow_basename=False):
    from chef.guards.adapters import root_matches

    roots = entry.get("roots") or []
    if any(isinstance(root, str) and root_matches(folder, root) for root in roots):
        return True
    if identities_match(folder, entry.get("identities") or []):
        return True
    if allow_basename and slot_name:
        for root in roots:
            if isinstance(root, str) and os.path.basename(root.rstrip("/\\")) == slot_name:
                return True
    return False


def associate_entry(entries, slot, name):
    matches = [entry for entry in entries if entry_matches(slot, entry, slot_name=name, allow_basename=True)]
    if len(matches) == 1:
        return matches[0]
    exact = [entry for entry in entries if entry_matches(slot, entry, slot_name=name, allow_basename=False)]
    if len(exact) == 1:
        return exact[0]
    return None


def merge_entry_identities(entry, identities):
    existing = [item for item in (entry.get("identities") or []) if isinstance(item, dict)]
    seen = {(item.get("kind"), item.get("value")) for item in existing}
    merged = list(existing)
    for item in identities:
        key = (item.get("kind"), item.get("value"))
        if key in seen or not item.get("kind") or not item.get("value"):
            continue
        seen.add(key)
        merged.append(item)
    entry["identities"] = merged
    return entry


def record_slot_registry(config, name):
    """Write the starter-resolved registry into the slot and refresh identity fields."""
    from chef.guards import adapters

    slot = config.slot_path(name)
    runtime = getattr(config, "runtime", "claude")
    registry = adapters.resolve_registry(runtime, project=slot)
    identities = project_identities(
        slot,
        p4_client=config.client_of(name) if getattr(getattr(config, "vcs", None), "has_workspaces", False) else read_p4_client(slot),
        p4_root=slot,
        project_id=getattr(config, "project_name", ""),
    )
    payload = {
        "registry": registry.path,
        "registrySource": registry.source,
        "runtime": runtime,
        "slot": name,
        "project": os.path.abspath(getattr(config, "main", "") or os.path.dirname(os.path.dirname(slot))),
        "identities": identities,
        "adapter": "",
        "adapterPath": "",
    }
    if registry.path and os.path.isdir(registry.path):
        registry_file = os.path.join(registry.path, adapters.REGISTRY_FILE)
        try:
            entries = adapters.load_registry(registry.path) if os.path.isfile(registry_file) else []
        except adapters.GuardOff:
            entries = []
        entry = associate_entry(entries, slot, name)
        if entry is not None:
            merge_entry_identities(entry, identities)
            payload["adapter"] = entry["adapter"]
            payload["adapterPath"] = os.path.join(registry.path, entry["adapter"], "adapter.json")
            atomic_write_text(
                registry_file,
                json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
            )
    if not payload["registry"]:
        # In-tree adapter projects have no registry to persist. chef_slot then
        # uses the planted adapter or fails loudly with the paths it tried.
        return ""
    return write_slot_binding(slot, payload)
