"""Shared workspace configuration, independent of mission/activity collection."""

import os
import threading
from dataclasses import dataclass

from chef.dashboard import discovery
from chef.guards import mission as mission_guard
from chef.infrastructure.errors import UsageError
from chef.infrastructure.paths import request_realpaths

INDEX_LOCK = threading.Lock()


def stamp(path, identity=False):
    try:
        info = os.stat(path)
        key = (os.path.realpath(path), info.st_dev, info.st_ino, info.st_mode)
        return key if identity else (*key, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as error:
        return (type(error), error.errno, getattr(error, "winerror", None))


class Inputs:
    def __init__(self):
        self.paths = {}
        self.state_dirs = {}

    def add(self, path, *, identity=False):
        key = (os.path.abspath(path), identity)
        if key not in self.paths:
            self.paths[key] = stamp(*key)

    def unchanged(self):
        return all(stamp(*key) == value for key, value in self.paths.items()) and all(
            os.path.normcase(os.path.realpath(path)) == target for path, target in self.state_dirs.items()
        )


@dataclass(frozen=True)
class WorkspaceCatalog:
    workspaces: tuple
    by_key: dict
    problems: tuple


class WorkspaceIndex:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.inputs = None
        self.catalog = None
        self.registry = None

    def get(self):
        with self.lock:
            try:
                registry = mission_guard.resolve_registry(self.config.runtime).path
            except mission_guard.GuardOff as error:
                registry = (str(error),)
            if self.inputs is not None and registry == self.registry and self.inputs.unchanged():
                return self.catalog
            # Never serve a previous permission snapshot after invalidation or a
            # failed rebuild. A configuration changing during discovery is retried.
            self.inputs = None
            self.catalog = None
            for _ in range(2):
                inputs = Inputs()
                with discovery.watching(inputs), request_realpaths():
                    workspaces, problems = discovery.discover(self.config)
                    for workspace in workspaces:
                        inputs.add(workspace.root, identity=True)
                        # Creating a mission store at its configured path does not change the layout.
                        inputs.state_dirs[workspace.state_dir] = os.path.normcase(os.path.realpath(workspace.state_dir))
                if inputs.unchanged():
                    self.registry = registry
                    self.catalog = WorkspaceCatalog(
                        tuple(workspaces), {item.key: item for item in workspaces}, tuple(problems)
                    )
                    self.inputs = inputs
                    return self.catalog
            raise UsageError("문서 접근 설정이 변경 중입니다. 다시 열어 주세요.")


def catalog(config):
    # The server primes this before accepting requests; lazy construction also
    # supports callers using route()/view_response() without an HTTP server.
    with INDEX_LOCK:
        index = getattr(config, "workspace_index", None)
        if index is None:
            index = config.workspace_index = WorkspaceIndex(config)
    return index.get()


def workspaces(config):
    return catalog(config).workspaces
