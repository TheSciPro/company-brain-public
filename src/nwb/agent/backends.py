"""The agent's virtual filesystem: one ``CompositeBackend`` with three routes (deepagents backends).

- ``/memories/`` -> ``MemoriesBackend``: USER.md / MEMORY.md in the per-user store, dated facts under
  ``/facts/`` from Graphiti. One route, because a longer child route would not show up in ``ls /memories/``.
- ``/org/``      -> read-only ``FilesystemBackend`` over ``agent/`` (AGENTS.md: org operating rules).
- ``/skills/``   -> read-only ``FilesystemBackend`` over ``agent/skills`` (SKILL.md, progressive disclosure).
- everything else -> ``StateBackend``: per-thread scratch (todo notes, offloaded tool results, summaries).

Writes to ``/org/`` and ``/skills/`` are denied by ``FilesystemPermission`` rules from ``config/agent.yaml``
(first match wins, deny first). The main agent uses this composite directly.

Subagents do not. ``build_subagents`` wraps it in ``WritePolicyBackend`` unless the subagent opts into the
parent's writes (``read_only: false`` and no ``writes`` allowlist). The wrapper is what a delegated task's
``write`` / ``edit`` actually hit, so hiding the tool is not the only guard.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import (
    PERMISSION_DENIED,
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import FilesystemPermission, _check_fs_permission

from nwb.agent.memories_backend import MemoriesBackend
from nwb.memory.guards import MemoryWriteGate
from nwb.memory.store import PersonalMemory

MEMORIES = "/memories/"
ORG = "/org/"
SKILLS = "/skills/"


class WritePolicyBackend(BackendProtocol):
    """Delegates to ``inner``. A write, edit, delete or upload runs only when ``rules`` allow it.

    Same first-match rules as ``FilesystemMiddleware`` (``_check_fs_permission``), checked on the
    path ``validate_path`` would hand the tool. A deny here is the backend result, so a caller that
    skips the tool still cannot change the file. Reads are forwarded unchanged.
    """

    def __init__(self, inner: BackendProtocol, rules: Sequence[FilesystemPermission]) -> None:
        self._inner = inner
        self._rules = list(rules)

    def _gate(self, path: str) -> tuple[str | None, str | None]:
        """Return ``(normalized, error)``. ``error`` is set when the write must not reach ``inner``."""
        try:
            normalized = validate_path(path)
        except ValueError as exc:
            return None, f"Error: {exc}"
        if _check_fs_permission(self._rules, "write", normalized) == "deny":
            return None, f"Error: permission denied for write on {normalized}"
        return normalized, None

    def ls(self, path: str) -> LsResult:
        return self._inner.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._inner.read(file_path, offset=offset, limit=limit)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return self._inner.grep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._inner.glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        normalized, error = self._gate(file_path)
        if error is not None or normalized is None:
            return WriteResult(error=error or "Error: permission denied for write")
        return self._inner.write(normalized, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        normalized, error = self._gate(file_path)
        if error is not None or normalized is None:
            return EditResult(error=error or "Error: permission denied for write")
        return self._inner.edit(normalized, old_string, new_string, replace_all=replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        normalized, error = self._gate(file_path)
        if error is not None or normalized is None:
            return DeleteResult(error=error or "Error: permission denied for write")
        return self._inner.delete(normalized)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        pending: list[tuple[int, str, bytes]] = []
        filled: dict[int, FileUploadResponse] = {}
        for index, (path, content) in enumerate(files):
            normalized, error = self._gate(path)
            if error is not None or normalized is None:
                filled[index] = FileUploadResponse(path=path, error=PERMISSION_DENIED)
                continue
            pending.append((index, normalized, content))
        if pending:
            uploaded = self._inner.upload_files([(path, content) for _, path, content in pending])
            for (index, _, _), response in zip(pending, uploaded, strict=True):
                filled[index] = response
        return [filled[index] for index in range(len(files))]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._inner.download_files(paths)


def build_backend(agent_dir: Path, memory: PersonalMemory, profile_gate: MemoryWriteGate) -> CompositeBackend:
    """The backend passed to ``create_deep_agent``. The store itself is the agent's ``store=`` (no global state)."""
    return CompositeBackend(
        default=StateBackend(),
        routes={
            MEMORIES: MemoriesBackend(memory, profile_gate),
            ORG: FilesystemBackend(root_dir=str(agent_dir.resolve()), virtual_mode=True),
            SKILLS: FilesystemBackend(root_dir=str((agent_dir / "skills").resolve()), virtual_mode=True),
        },
    )
