"""Reusable document boundaries, independent of mission/activity collection."""

import os
import threading

from chef.dashboard import discovery
from chef.guards import mission as mission_guard
from chef.infrastructure.errors import UsageError

INDEX_LOCK = threading.Lock()


def stamp(path, identity=False):
    try:
        info = os.stat(path)
        key = (os.path.realpath(path), info.st_dev, info.st_ino, info.st_mode)
        return key if identity else (*key, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except (FileNotFoundError, NotADirectoryError):
        return None


class Inputs:
    def __init__(self):
        self.paths = {}

    def add(self, path, *, identity=False):
        key = (os.path.abspath(path), identity)
        if key not in self.paths:
            self.paths[key] = stamp(*key)

    def unchanged(self):
        return all(stamp(*key) == value for key, value in self.paths.items())


class WorkspaceIndex:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.inputs = None
        self.workspaces = ()
        self.registry = None

    def get(self):
        with self.lock:
            registry = mission_guard.resolve_registry(self.config.runtime).path
            if self.inputs is not None and registry == self.registry and self.inputs.unchanged():
                return self.workspaces
            # Never serve a previous permission snapshot after invalidation or a
            # failed rebuild. A configuration changing during discovery is retried.
            self.inputs = None
            self.workspaces = ()
            for _ in range(2):
                inputs = Inputs()
                with discovery.watching(inputs):
                    workspaces, _ = discovery.discover(self.config)
                    for workspace in workspaces:
                        inputs.add(workspace.root, identity=True)
                        inputs.add(workspace.state_dir, identity=True)
                if inputs.unchanged():
                    self.registry = registry
                    self.workspaces = tuple(workspaces)
                    self.inputs = inputs
                    return self.workspaces
            raise UsageError("문서 접근 설정이 변경 중입니다. 다시 열어 주세요.")


def workspaces(config):
    # The server primes this before accepting requests; lazy construction also
    # supports callers using route()/view_response() without an HTTP server.
    with INDEX_LOCK:
        index = getattr(config, "document_index", None)
        if index is None:
            index = config.document_index = WorkspaceIndex(config)
    return index.get()
