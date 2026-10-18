"""``/memories/`` for Deep Agents built-in file tools.

``CompositeBackend`` keeps the longest matching route, so a child route ``/memories/facts/`` would hide
``/memories/`` from ``ls``. This one backend serves both homes:

- ``USER.md``, ``MEMORY.md`` and any other path: the per-user ``StoreBackend`` (preferences and directives);
- ``/facts/<id>.md``: dated Graphiti facts. ``grep`` is a literal scan of those files. Semantic recall for
  the runtime block goes through ``PersonalMemory.recall``, which applies slots, as-of and the latency budget.

New files under ``/facts/`` append an episode. Editing or deleting a fact file is refused; the old fact
stays. Identity comes from ``runtime.context``. With no context the call returns an error and does not guess.
A Graphiti failure on a fact path returns an error result and is logged; it does not raise into the tool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.store import StoreBackend
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    _glob_search_files,
    create_file_data,
    grep_matches_from_files,
    slice_read_response,
)
from langgraph.runtime import get_runtime

from nwb.agent.context import RuntimeContext, user_namespace
from nwb.errors import NotFoundError
from nwb.identity import Actor
from nwb.memory.guards import MemoryWriteGate
from nwb.memory.store import DatedFact, PersonalMemory
from nwb.obs.log import get_logger

log = get_logger(__name__)

_FACTS = "/facts"
_APPEND_ONLY = "facts are append-only; write a new file under /facts/. The previous fact was left unchanged"
_NO_CONTEXT = "/memories/ needs a RuntimeContext; refusing to guess whose memory this is"
_UNAVAILABLE = "personal memory is unavailable"
_TIMED_OUT = "personal memory timed out"


class MemoriesBackend(BackendProtocol):
    def __init__(self, memory: PersonalMemory, profile_gate: MemoryWriteGate) -> None:
        self._memory = memory
        self._files = StoreBackend(namespace=user_namespace)
        self._profile = profile_gate

    def ls(self, path: str) -> LsResult:
        actor = self._actor()
        if actor is None:
            return LsResult(error=_NO_CONTEXT)
        if _is_facts(path):
            try:
                facts = self._current(actor)
            except Exception as exc:
                return LsResult(error=self._store_error(actor, exc))
            return LsResult(entries=[_info(f"/facts/{fact.id}.md", fact) for fact in facts])
        try:
            listed = self._files.ls(path)
        except PermissionError as exc:
            return LsResult(error=str(exc))
        if listed.error or _norm(path) not in {"/", ""}:
            return listed
        entries = list(listed.entries or [])
        if not any(str(e.get("path", "")).rstrip("/") == "/facts" for e in entries):
            entries.append({"path": "/facts/", "is_dir": True})
        return LsResult(entries=entries)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        actor = self._actor()
        if actor is None:
            return ReadResult(error=_NO_CONTEXT)
        fact_id = _fact_id(file_path)
        if fact_id is None and _is_facts(file_path):
            return ReadResult(error="fact not found")
        if fact_id is not None:
            try:
                fact = self._memory.read_fact(actor, fact_id)
            except NotFoundError:
                return ReadResult(error="fact not found")
            except Exception as exc:
                return ReadResult(error=self._store_error(actor, exc))
            return slice_read_response(cast(FileData, create_file_data(_render(fact))), offset, limit)
        try:
            return self._files.read(file_path, offset, limit)
        except PermissionError as exc:
            return ReadResult(error=str(exc))

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ) -> GrepResult:
        actor = self._actor()
        if actor is None:
            return GrepResult(error=_NO_CONTEXT)
        target = path or "/"
        if _is_facts(target):
            files = self._fact_files_or_error(actor)
            if isinstance(files, str):
                return GrepResult(error=files)
            return grep_matches_from_files(files, pattern, path, glob, max_count=max_count)
        if target not in {"/", ""}:
            try:
                return self._files.grep(pattern, path, glob, max_count=max_count)
            except PermissionError as exc:
                return GrepResult(error=str(exc))
        # Root searches both homes, then applies max_count once. Capping each side and
        # concatenating would return up to twice max_count and OR their truncated flags.
        files = self._fact_files_or_error(actor)
        if isinstance(files, str):
            return GrepResult(error=files)
        fact_hits = grep_matches_from_files(files, pattern, "/facts", glob, max_count=None)
        if fact_hits.error:
            return fact_hits
        try:
            file_hits = self._files.grep(pattern, path, glob, max_count=None)
        except PermissionError as exc:
            return GrepResult(error=str(exc))
        if file_hits.error:
            return file_hits
        matches = list(fact_hits.matches or []) + list(file_hits.matches or [])
        truncated = False
        if max_count is not None and len(matches) > max_count:
            matches = matches[:max_count]
            truncated = True
        return GrepResult(matches=matches, truncated=truncated)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        actor = self._actor()
        if actor is None:
            return GlobResult(error=_NO_CONTEXT)
        try:
            listed = self._files.glob(pattern, path)
        except PermissionError as exc:
            return GlobResult(error=str(exc))
        if listed.error:
            return listed
        matches = list(listed.matches or [])
        # Same contract as StoreBackend: a pattern with no slash matches the basename
        # at any depth under ``path``. ``*.md`` under ``/notes`` must not return facts.
        try:
            current = self._current(actor)
        except Exception as exc:
            return GlobResult(error=self._store_error(actor, exc))
        files = {
            f"/facts/{fact.id}.md": {"modified_at": _info(f"/facts/{fact.id}.md", fact)["modified_at"]}
            for fact in current
        }
        try:
            found = _glob_search_files(files, pattern, path)
        except InvalidGlobPatternError as exc:
            return GlobResult(error=str(exc))
        if found != "No files found":
            by_id = {fact.id: fact for fact in current}
            for raw in found.splitlines():
                fact_id = _fact_id(raw)
                fact = by_id.get(fact_id) if fact_id else None
                if fact is not None:
                    matches.append(_info(raw, fact))
        return GlobResult(matches=matches, truncated=listed.truncated, truncation_reason=listed.truncation_reason)

    def write(self, file_path: str, content: str) -> WriteResult:
        actor = self._actor()
        if actor is None:
            return WriteResult(error=_NO_CONTEXT)
        if _is_facts(file_path):
            try:
                return self._write_fact(actor, file_path, content)
            except Exception as exc:
                return WriteResult(error=self._store_error(actor, exc))
        if reason := self._profile.reject(file_path, content):
            return WriteResult(error=reason)
        try:
            return self._files.write(file_path, content)
        except PermissionError as exc:
            return WriteResult(error=str(exc))

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if _is_facts(file_path):
            return EditResult(error=_APPEND_ONLY)
        actor = self._actor()
        if actor is None:
            return EditResult(error=_NO_CONTEXT)
        if reason := self._profile.reject(file_path, new_string):
            return EditResult(error=reason)
        try:
            return self._files.edit(file_path, old_string, new_string, replace_all)
        except PermissionError as exc:
            return EditResult(error=str(exc))

    def delete(self, file_path: str) -> DeleteResult:
        if _is_facts(file_path):
            return DeleteResult(error=_APPEND_ONLY)
        return DeleteResult(error="delete is disabled")

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """MemoryMiddleware loads ``/memories/USER.md`` and ``MEMORY.md`` through this."""
        out: list[FileDownloadResponse | None] = [None] * len(paths)
        store_at: list[int] = []
        store_paths: list[str] = []
        for i, path in enumerate(paths):
            fact_id = _fact_id(path)
            if fact_id is not None:
                out[i] = self._download_fact(path, fact_id)
            elif _is_facts(path):
                out[i] = FileDownloadResponse(path=path, content=None, error="file_not_found")
            else:
                store_at.append(i)
                store_paths.append(path)
        if store_paths:
            for i, response in zip(store_at, self._files.download_files(store_paths), strict=True):
                out[i] = response
        return [response for response in out if response is not None]

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out: list[FileUploadResponse | None] = [None] * len(files)
        store_at: list[int] = []
        store_files: list[tuple[str, bytes]] = []
        for i, (path, content) in enumerate(files):
            if _is_facts(path):
                written = self.write(path, content.decode("utf-8", errors="replace"))
                out[i] = FileUploadResponse(path=path, error=written.error)
            else:
                text = content.decode("utf-8", errors="replace")
                if reason := self._profile.reject(path, text):
                    out[i] = FileUploadResponse(path=path, error=reason)
                else:
                    store_at.append(i)
                    store_files.append((path, content))
        if store_files:
            stored = self._files.upload_files(store_files)
            for i, response in zip(store_at, stored, strict=True):
                out[i] = response
        return [response for response in out if response is not None]

    def _download_fact(self, path: str, fact_id: str) -> FileDownloadResponse:
        actor = self._actor()
        if actor is None:
            return FileDownloadResponse(path=path, content=None, error=_NO_CONTEXT)
        try:
            fact = self._memory.read_fact(actor, fact_id)
        except NotFoundError:
            return FileDownloadResponse(path=path, content=None, error="file_not_found")
        except Exception as exc:
            return FileDownloadResponse(path=path, content=None, error=self._store_error(actor, exc))
        return FileDownloadResponse(path=path, content=_render(fact).encode("utf-8"), error=None)

    def _write_fact(self, actor: Actor, file_path: str, content: str) -> WriteResult:
        fact_id = _fact_id(file_path)
        if fact_id and self._exists(actor, fact_id):
            return WriteResult(error=_APPEND_ONLY)
        when = datetime(actor.asof.year, actor.asof.month, actor.asof.day, tzinfo=UTC)
        receipt = self._memory.append_fact(actor, content, content, when)
        if receipt.rejected or not receipt.episode_id:
            return WriteResult(error=receipt.rejected[0] if receipt.rejected else "fact was not stored")
        # Graphiti paraphrases the edge text, so the submitted sentence is not a reliable
        # match. Confirm with the edge uuids this episode wrote.
        current = {fact.id: fact for fact in self._current(actor)}
        chosen = next((current[fact_id] for fact_id in reversed(receipt.fact_ids) if fact_id in current), None)
        if chosen is None:
            return WriteResult(error="fact was not stored")
        return WriteResult(path=f"/facts/{chosen.id}.md")

    def _current(self, actor: Actor) -> list[DatedFact]:
        return self._memory.facts_as_of(actor, include_history=False)

    def _fact_files_or_error(self, actor: Actor) -> dict[str, Any] | str:
        try:
            facts = self._current(actor)
        except Exception as exc:
            return self._store_error(actor, exc)
        return {f"/facts/{fact.id}.md": create_file_data(_render(fact)) for fact in facts}

    def _store_error(self, actor: Actor, exc: Exception) -> str:
        log.warning("fact store failed", person=actor.person_id, error=type(exc).__name__, exc_info=exc)
        if isinstance(exc, TimeoutError):
            return _TIMED_OUT
        return _UNAVAILABLE

    def _exists(self, actor: Actor, fact_id: str) -> bool:
        try:
            self._memory.read_fact(actor, fact_id)
        except NotFoundError:
            return False
        return True

    def _actor(self) -> Actor | None:
        runtime: Any = get_runtime()
        ctx = getattr(runtime, "context", None)
        if not isinstance(ctx, RuntimeContext):
            return None
        return ctx.actor()


def _norm(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _is_facts(path: str) -> bool:
    path = _norm(path).rstrip("/") or "/"
    return path == _FACTS or path.startswith(f"{_FACTS}/")


def _fact_id(path: str) -> str | None:
    path = _norm(path)
    prefix = "/facts/"
    if not path.startswith(prefix) or path.endswith("/"):
        return None
    name = path[len(prefix) :]
    if "/" in name:
        return None
    return name[:-3] if name.endswith(".md") else name


def _render(fact: DatedFact) -> str:
    valid = fact.valid_at.date().isoformat() if fact.valid_at else ""
    invalid = fact.invalid_at.date().isoformat() if fact.invalid_at else ""
    return (
        f"id: {fact.id}\nkind: {fact.kind}\nname: {fact.name}\n"
        f"valid_at: {valid}\ninvalid_at: {invalid}\n"
        f"supersedes: {fact.supersedes or ''}\nsuperseded_by: {fact.superseded_by or ''}\n\n{fact.text}\n"
    )


def _info(path: str, fact: DatedFact) -> FileInfo:
    modified = (fact.invalid_at or fact.valid_at or datetime.now(UTC)).isoformat()
    return {"path": path, "is_dir": False, "size": len(fact.text), "modified_at": modified}
