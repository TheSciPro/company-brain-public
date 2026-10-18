"""Regression tests for /memories/ file tools.

A fact write confirms by the episode's edge ids. Graphiti paraphrases the stored sentence, so a
substring of the submitted text can hit an older fact and miss the one just written. Glob follows
the same path scope as the store. Grep at / applies max_count once across both homes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from deepagents.backends.protocol import GlobResult, GrepResult
from langchain_core.runnables.config import var_child_runnable_config
from langgraph._internal._constants import CONF, CONFIG_KEY_RUNTIME
from langgraph.runtime import DEFAULT_RUNTIME

from nwb.agent.context import RuntimeContext
from nwb.agent.memories_backend import MemoriesBackend
from nwb.errors import NotFoundError
from nwb.identity import Actor
from nwb.memory.store import DatedFact, EpisodeReceipt

_WHEN = datetime(2026, 9, 1, tzinfo=UTC)
_SUBMITTED = "I work on the Payments team."


class _Allow:
    """These tests exercise fact files. Profile writes are not under test, so the gate allows them."""

    def reject(self, path: str, text: str) -> str | None:
        return None


def _backend(memory: object) -> MemoriesBackend:
    return MemoriesBackend(memory, _Allow())  # type: ignore[arg-type]


def _fact(fact_id: str, text: str) -> DatedFact:
    return DatedFact(id=fact_id, text=text, kind="fact", name="ABOUT", valid_at=_WHEN, invalid_at=None)


class _ParaphraseMemory:
    """Stores a rewritten edge, plus an older fact whose text is the submitted sentence."""

    def __init__(self) -> None:
        self.calls = 0
        self._facts = [_fact("old-1", _SUBMITTED)]

    def append_fact(
        self, actor: Actor, content: str, source_text: str | None, reference_time: datetime
    ) -> EpisodeReceipt:
        self.calls += 1
        self._facts.append(_fact("edge-9", "User works on the Payments team"))
        return EpisodeReceipt("ep-1", [], [], 1, ["edge-9"])

    def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]:
        return list(self._facts)

    def read_fact(self, actor: Actor, fact_id: str) -> DatedFact:
        found = next((fact for fact in self._facts if fact.id == fact_id), None)
        if found is None:
            raise NotFoundError("fact not found")
        return found


class _Listing:
    def __init__(self, facts: list[DatedFact]) -> None:
        self._facts = facts
        self.grep_limits: list[int | None] = []

    def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]:
        return list(self._facts)

    def read_fact(self, actor: Actor, fact_id: str) -> DatedFact:
        found = next((fact for fact in self._facts if fact.id == fact_id), None)
        if found is None:
            raise NotFoundError("fact not found")
        return found


class _Store:
    def __init__(self) -> None:
        self.grep_limits: list[int | None] = []

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[{"path": "/notes/a.md", "is_dir": False}])

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ) -> GrepResult:
        self.grep_limits.append(max_count)
        return GrepResult(
            matches=[
                {"path": "/USER.md", "line": 1, "text": "alpha one"},
                {"path": "/MEMORY.md", "line": 1, "text": "alpha two"},
            ]
        )


@contextmanager
def _as_user() -> Iterator[None]:
    ctx = RuntimeContext("acme", "u1", "s", "r", "2026-09-28", "")
    token = var_child_runnable_config.set({CONF: {CONFIG_KEY_RUNTIME: replace(DEFAULT_RUNTIME, context=ctx)}})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


def test_fact_write_confirms_the_episode_edge_not_a_text_substring() -> None:
    memory = _ParaphraseMemory()
    backend = _backend(memory)
    with _as_user():
        result = backend.write("/facts/note.md", _SUBMITTED)
    assert result.error is None
    assert result.path == "/facts/edge-9.md"
    assert memory.calls == 1
    assert _SUBMITTED not in "User works on the Payments team"


def test_glob_keeps_facts_inside_the_requested_directory() -> None:
    memory = _Listing([_fact("f1", "alpha team")])
    backend = _backend(memory)
    backend._files = _Store()  # type: ignore[assignment]
    with _as_user():
        scoped = backend.glob("*.md", "/notes")
        root = backend.glob("*.md", "/")
        facts = backend.glob("*.md", "/facts")
    scoped_paths = [item["path"] for item in scoped.matches or []]
    assert "/facts/f1.md" not in scoped_paths
    assert "/notes/a.md" in scoped_paths
    assert "/facts/f1.md" in [item["path"] for item in root.matches or []]
    assert "/facts/f1.md" in [item["path"] for item in facts.matches or []]


class _Down:
    """Graphiti is unreachable. Every fact call raises; the tool must not."""

    def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]:
        raise ConnectionError("falkor down")

    def read_fact(self, actor: Actor, fact_id: str) -> DatedFact:
        raise ConnectionError("falkor down")

    def append_fact(
        self, actor: Actor, content: str, source_text: str | None, reference_time: datetime
    ) -> EpisodeReceipt:
        raise ConnectionError("falkor down")


def test_fact_tools_return_errors_when_the_store_fails(caplog: pytest.LogCaptureFixture) -> None:
    backend = _backend(_Down())
    backend._files = _Store()  # type: ignore[assignment]
    with _as_user(), caplog.at_level(logging.WARNING, logger="nwb.agent.memories_backend"):
        listed = backend.ls("/facts")
        read = backend.read("/facts/edge-9.md")
        found = backend.grep("alpha", "/facts")
        matched = backend.glob("*.md", "/facts")
        wrote = backend.write("/facts/note.md", _SUBMITTED)
    for result in (listed, read, found, matched, wrote):
        assert result.error == "personal memory is unavailable"
        assert "falkor down" not in result.error
    assert "fact store failed" in caplog.text


def test_fact_tool_timeout_says_timed_out_not_empty() -> None:
    class _Slow:
        def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]:
            raise TimeoutError()

    backend = _backend(_Slow())
    with _as_user():
        listed = backend.ls("/facts")
    assert listed.error == "personal memory timed out"
    assert listed.entries is None


def test_root_grep_applies_max_count_once() -> None:
    memory = _Listing([_fact("f1", "alpha one"), _fact("f2", "alpha two")])
    store = _Store()
    backend = _backend(memory)
    backend._files = store  # type: ignore[assignment]
    with _as_user():
        result = backend.grep("alpha", "/", max_count=2)
    assert result.error is None
    assert len(result.matches or []) == 2
    assert result.truncated is True
    assert store.grep_limits == [None]
