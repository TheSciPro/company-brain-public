"""Filesystem gate for an unattended consolidation run.

Adds are passed through. A patch or a removal is handed to ``stage`` and the file is left as it was.
``delete`` never reaches the inner backend. This is the code guard; the memory-keeper prompt says the
same thing, and the prompt is not the guard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from deepagents.backends.protocol import (
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

from nwb.memory.consolidate import added_are_directives, classify, index_lines_only, wrong_store
from nwb.obs.log import get_logger

log = get_logger(__name__)

Stage = Callable[[str, str, str, str], None]
_HELD = "{op} of {path} is waiting for approval; the file was not changed"


class AddOnlyBackend(BackendProtocol):
    """Wraps the memory backend used while the consolidation agent is running."""

    def __init__(self, inner: BackendProtocol, stage: Stage) -> None:
        self._inner = inner
        self._stage = stage
        self.added = 0
        self.staged = 0

    def reset(self) -> None:
        self.added = 0
        self.staged = 0

    def ls(self, path: str) -> LsResult:
        return self._inner.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._inner.read(file_path, offset, limit)

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ) -> GrepResult:
        return self._inner.grep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._inner.glob(pattern, path)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._inner.download_files(paths)

    def write(self, file_path: str, content: str) -> WriteResult:
        before = _text(self._inner.read(file_path, offset=0, limit=100_000))
        if before is None:
            reason = wrong_store(file_path, None, content)
            if reason:
                return WriteResult(error=reason)
            result = self._inner.write(file_path, content)
            if result.error is None:
                self.added += 1
            return result
        return self._finish(file_path, before, content, lambda: self._overwrite(file_path, before, content))

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        before = _text(self._inner.read(file_path, offset=0, limit=100_000))
        if before is None:
            return EditResult(error="file not found")
        after = _apply_edit(before, old_string, new_string, replace_all)
        if after is None:
            return EditResult(error="old_string was not found; the file was not changed")
        outcome = self._finish(
            file_path,
            before,
            after,
            lambda: self._inner.edit(file_path, old_string, new_string, replace_all),
        )
        if isinstance(outcome, EditResult):
            return outcome
        return EditResult(error=outcome.error, path=outcome.path)

    def delete(self, file_path: str) -> DeleteResult:
        before = _text(self._inner.read(file_path, offset=0, limit=100_000))
        if before is None:
            return DeleteResult(error="file not found")
        self._hold("remove", file_path, before, before)
        return DeleteResult(error=f"removal of {file_path} is waiting for approval; nothing was deleted")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out: list[FileUploadResponse] = []
        for path, raw in files:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                out.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            result = self.write(path, text)
            out.append(FileUploadResponse(path=path, error="permission_denied" if result.error else None))
        return out

    def _overwrite(self, path: str, before: str, after: str) -> WriteResult:
        edited = self._inner.edit(path, before, after, False)
        if edited.error is None:
            return WriteResult(path=edited.path or path)
        return self._inner.write(path, after)

    def _finish(self, path: str, before: str, after: str, apply: Callable[[], Any]) -> WriteResult | EditResult:
        if _meaningful_same(before, after):
            return WriteResult(path=path)
        reason = wrong_store(path, before, after)
        if reason:
            return WriteResult(error=reason)
        kind = classify(before, after)
        if kind == "add" and path.endswith("USER.md") and not added_are_directives(before, after):
            kind = "patch"
        if kind == "add" and path.endswith("MEMORY.md") and not index_lines_only(before, after):
            return WriteResult(error="MEMORY.md only takes an index line: a date and a /memories/facts/ path")
        if kind != "add":
            self._hold(kind, path, before, after)
            return WriteResult(error=_HELD.format(op=kind, path=path))
        result = apply()
        if getattr(result, "error", None) is None:
            self.added += 1
        return result

    def _hold(self, op: str, path: str, before: str, after: str) -> None:
        self._stage(op, path, before, after)
        self.staged += 1
        log.info("consolidation held a memory change", op=op, path=path)


def _text(result: ReadResult) -> str | None:
    if result.error or result.file_data is None:
        return None
    content: Any = result.file_data.get("content")
    if isinstance(content, list):
        return "\n".join(str(line) for line in content)
    return str(content)


def _apply_edit(text: str, old: str, new: str, replace_all: bool) -> str | None:
    if old not in text:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _meaningful_same(before: str, after: str) -> bool:
    return classify(before, after) == "add" and before == after
