"""Explicit, hash-bound read sources and project role definitions for native jobs."""

import hashlib
import os
import re
import stat
import tomllib
from contextlib import ExitStack
from pathlib import Path

from chef.guards import mission
from chef.infrastructure.configuration import read_adapter

SOURCE_LIMIT = 2 * 1024 * 1024


def native_routing(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"model", "provider"} or any(
            not isinstance(item, str) or not item.strip() or "\x00" in item for item in value.values()):
        raise ValueError("native role routing accepts nonempty model and provider strings only")
    return dict(value)


def settings_for(cwd):
    located = mission.locate_adapter({"cwd": str(cwd), "han_runtime": "hermes"})
    return read_adapter(located.adapter_path, "hermes").get("workers", {}) if located else {}


class ReadSources:
    def __init__(self, values=None):
        values = {} if values is None else values
        if not isinstance(values, dict) or len(values) > 512:
            raise ValueError("workers.readOnlySources must be a bounded path/hash object")
        self.files = {}
        for value, fingerprint in values.items():
            if (not isinstance(value, str) or not os.path.isabs(value) or "\x00" in value
                    or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                raise ValueError("read-only sources need absolute paths and SHA-256 hashes")
            path = Path(value)
            if str(path) != os.path.normpath(value) or path.resolve(strict=True) != path:
                raise ValueError("read-only source paths must be canonical and cannot follow links")
            self.files[str(path)] = fingerprint

    def contains(self, value):
        return isinstance(value, (str, Path)) and str(value) in self.files

    def bytes(self, value):
        path = Path(value)
        expected = self.files.get(str(path))
        if expected is None:
            raise ValueError("this file is not an approved read-only source")
        if path.resolve(strict=True) != path:
            raise ValueError("read-only sources cannot follow links")
        with ExitStack() as stack:
            if os.name == "posix":
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                directory = os.open(path.anchor, flags)
                stack.callback(os.close, directory)
                for part in path.parts[1:-1]:
                    directory = os.open(part, flags, dir_fd=directory)
                    stack.callback(os.close, directory)
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                stream = stack.enter_context(os.fdopen(fd, "rb"))
            else:
                stream = stack.enter_context(path.open("rb"))
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("read-only sources must be regular text files")
            data = stream.read(SOURCE_LIMIT + 1)
        if len(data) > SOURCE_LIMIT or b"\x00" in data or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("read-only source changed, is too large, or is not text: " + str(path))
        return data

    def read(self, value, offset=0, limit=64 * 1024):
        data = self.bytes(value)
        end = min(len(data), offset + limit)
        return {"text": data[offset:end].decode("utf-8-sig" if offset == 0 else "utf-8", "replace"),
                "truncated": end < len(data), "next_offset": end}

    def validate(self):
        for path in self.files:
            self.bytes(path)


def role_contract(settings, cwd, role, routing=None):
    """Read the actual generated definition; instructions never set launch policy implicitly."""
    routing = native_routing(routing)
    definitions = settings.get("roleContracts", {})
    if not isinstance(definitions, dict):
        raise ValueError("workers.roleContracts must be an object")
    contract = definitions.get(role, definitions.get("chef:" + role))
    if contract is None:
        return routing, ""
    if (not isinstance(contract, dict) or set(contract) != {"path", "sha256", "modelPolicy"}
            or contract["modelPolicy"] not in ("definition", "inherit")):
        raise ValueError("role contracts require path, sha256 and modelPolicy")
    relative = contract["path"]
    if (not isinstance(relative, str) or not relative or os.path.isabs(relative)
            or any(part in ("", ".", "..", ".git") for part in relative.split("/")) or "\\" in relative):
        raise ValueError("role instructions must use a workspace-relative path")
    root = Path(cwd).resolve(strict=True)
    path = root / relative
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("role instructions escaped the workspace")
    source = ReadSources({str(path): contract["sha256"]})
    definition = tomllib.loads(source.bytes(path).decode("utf-8-sig"))
    body = definition.get("developer_instructions")
    if not isinstance(body, str) or not body.strip() or len(body) > 24000:
        raise ValueError("role instructions must contain bounded developer_instructions")
    declared = {key: definition[key] for key in ("model", "model_reasoning_effort") if key in definition}
    if contract["modelPolicy"] == "inherit" and declared:
        raise ValueError("an inherited role must not retain fixed model or effort settings")
    if declared.get("model_reasoning_effort"):
        raise ValueError("Hermes public lifecycle does not support the role's fixed model_reasoning_effort; "
                         "use a supporting backend or explicitly review the role policy")
    if "model" in declared:
        model = declared["model"]
        if not isinstance(model, str) or not model.strip() or "\x00" in model or ("model" in routing and routing["model"] != model):
            raise ValueError("Hermes routing conflicts with the fixed role model")
        routing["model"] = model
    return routing, body


def preflight(settings, cwd, roles):
    sources = ReadSources(settings.get("readOnlySources"))
    sources.validate()
    configured = settings.get("hermesRoles", {})
    if not isinstance(configured, dict):
        raise ValueError("workers.hermesRoles must be an object")
    for role in roles:
        try:
            role_contract(settings, cwd, role, configured.get(role, configured.get("chef:" + role)))
        except (OSError, ValueError) as error:
            raise ValueError(f"chef:{role}: {error}") from error
