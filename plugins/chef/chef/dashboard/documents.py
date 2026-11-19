"""dashboard / documents."""

import hashlib
import os

from chef.dashboard.contracts import SYSTEM_DOCUMENTS
from chef.infrastructure import paths as infrastructure_paths
from chef.mission import state as mission_state


def document_visible(folder, path, shared):
    root, real = infrastructure_paths.request_realpath(folder), infrastructure_paths.request_realpath(path)
    if not infrastructure_paths.inside_root(root, real):
        return False
    requested = os.path.relpath(os.path.abspath(path), os.path.abspath(folder)).replace(os.sep, "/")
    if any(part.startswith(".") and part not in (".", "..") for part in requested.split("/")):
        return False
    name = os.path.relpath(real, root).replace(os.sep, "/")
    if any(part.startswith(".") for part in name.split("/")) or name == mission_state.SHARED_DOCUMENTS_FILE:
        return False
    if infrastructure_paths.inside_root(root, os.path.abspath(path)):
        requested = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    if os.path.isfile(os.path.join(folder, "mission.md")) and mission_state.auto_question_document(
        folder, requested
    ):
        return True
    return name in SYSTEM_DOCUMENTS or any(
        os.path.normcase(infrastructure_paths.request_realpath(os.path.join(folder, item)))
        == os.path.normcase(real)
        for item in shared
    )


def digest(data):
    return hashlib.sha256(data).hexdigest()


def shared_bytes_match(folder, path, data):
    real = os.path.normcase(infrastructure_paths.request_realpath(path))
    return any(
        os.path.normcase(infrastructure_paths.request_realpath(os.path.join(folder, row["path"]))) == real
        and digest(data) == row["sha256"]
        for row in mission_state.shared_document_rows(folder)
    )


def read_shared_text(folder, path):
    name = os.path.relpath(path, folder).replace(os.sep, "/")
    if not document_visible(folder, path, {name}):
        return ""
    with open(path, "rb") as stream:
        data = stream.read()
    if mission_state.auto_question_document(folder, name) or shared_bytes_match(folder, path, data):
        return data.decode("utf-8-sig")
    return ""
