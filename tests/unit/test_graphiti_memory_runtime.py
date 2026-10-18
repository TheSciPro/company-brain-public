"""Regression tests for the Graphiti personal-memory runtime.

The client loop is one thread, so chat and consolidation can call it together. A search failure
degrades to an empty recall. Preference edges are named PREFERS, and recall drops that kind.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core", reason="graph extra is not installed; pip install -e '.[graph]'")

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import FactsConfig, GraphConfig, MemoryConfig
from nwb.identity import Actor
from nwb.memory.graphiti_memory import GraphitiPersonalMemory, _fact_ids
from tests.conftest import ROOT

_ASOF = date(2026, 9, 28)
_WHEN = datetime(2026, 9, 1, tzinfo=UTC)


class _QuietClient:
    async def close(self) -> None:
        return None


class _FailingSearch(_QuietClient):
    def _resolve_request_scope(self, group_id: str) -> tuple[str, object, None]:
        return group_id, object(), None

    async def search(self, *args: object, **kwargs: object) -> list[object]:
        raise ConnectionError("falkor down")


def _memory(client: object, cfg: MemoryConfig | None = None) -> GraphitiPersonalMemory:
    memory_cfg = cfg or MemoryConfig(version=1, kinds=("fact",))
    return GraphitiPersonalMemory(client, GraphConfig(version=1), memory_cfg, [])


def test_fact_ids_follow_the_episode_not_an_older_edge() -> None:
    older = SimpleNamespace(uuid="old-1", episodes=["earlier"])
    written = SimpleNamespace(uuid="edge-9", episodes=["ep-1"])
    assert _fact_ids("ep-1", [written], [older, written], {"old-1"}) == ["edge-9"]


def test_graphiti_calls_share_one_loop_thread() -> None:
    memory = _memory(_QuietClient())

    def _call() -> str:
        async def _pause() -> str:
            await asyncio.sleep(0.05)
            return "ok"

        return memory._run(_pause(), timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_call)
            second = pool.submit(_call)
            assert first.result(timeout=5) == "ok"
            assert second.result(timeout=5) == "ok"
        memory.close()
        memory.close()
    finally:
        memory.close()


def test_recall_failure_returns_no_memories(caplog: pytest.LogCaptureFixture) -> None:
    memory = _memory(_FailingSearch())
    actor = Actor("acme", "u1", _ASOF)
    memory._indexed.add(memory._group(actor))
    try:
        with caplog.at_level(logging.WARNING, logger="nwb.memory.graphiti_memory"):
            result = memory.recall(actor, "payments team", {"u1"}, 3)
    finally:
        memory.close()
    assert result.hits == []
    assert result.not_searched == ["graphiti"]
    assert result.skipped is True
    assert "recall failed" in result.note
    assert "falkor down" not in result.note
    assert "memory recall failed" in caplog.text


def test_prefers_edges_are_preference_kind_and_dropped_on_recall() -> None:
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig)
    graph = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    memory = GraphitiPersonalMemory(_QuietClient(), graph, cfg, [])
    actor = Actor("acme", "u1", _ASOF)
    prefers = SimpleNamespace(
        uuid="p1", name="PREFERS", fact="User prefers amounts in INR", valid_at=_WHEN, invalid_at=None
    )
    titled = SimpleNamespace(
        uuid="p2", name="Prefers", fact="User prefers short answers", valid_at=_WHEN, invalid_at=None
    )
    works = SimpleNamespace(
        uuid="w1", name="WORKS_ON", fact="User works on the Payments team", valid_at=_WHEN, invalid_at=None
    )
    try:
        assert memory._kind("PREFERS") == "preference"
        assert memory._kind("Prefers") == "preference"
        assert memory._kind("PREFERENCE") == "preference"
        assert memory._kind("WORKS_ON") == "relationship"
        hits = memory._hits([prefers, titled, works], actor, 5)
    finally:
        memory.close()
    assert [hit.fact.id for hit in hits] == ["w1"]


class _Driver:
    def clone(self, database: str) -> _Driver:
        return self


class _SlowWrite:
    def __init__(self) -> None:
        self.driver = _Driver()
        self.cancelled = threading.Event()

    async def add_episode(self, **kwargs: object) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def close(self) -> None:
        return None


class _SlowSearch(_QuietClient):
    def _resolve_request_scope(self, group_id: str) -> tuple[str, object, None]:
        return group_id, object(), None

    async def search(self, *args: object, **kwargs: object) -> list[object]:
        await asyncio.sleep(30)
        return []


def _short_timeouts(*, recall_ms: int, io_ms: int) -> MemoryConfig:
    return MemoryConfig(
        version=1,
        kinds=("fact",),
        facts=FactsConfig(latency_budget_ms=recall_ms, io_timeout_ms=io_ms),
    )


async def _no_edges(*_args: object, **_kwargs: object) -> list[object]:
    return []


def test_write_timeout_cancels_and_returns_a_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nwb.memory.graphiti_memory.EntityEdge.get_by_group_ids", _no_edges)
    client = _SlowWrite()
    memory = _memory(client, _short_timeouts(recall_ms=4000, io_ms=200))
    actor = Actor("acme", "u1", _ASOF)
    memory._indexed.add(memory._group(actor))
    try:
        started = time.perf_counter()
        receipt = memory.append_fact(actor, "I work on the Payments team.", None, _WHEN)
        elapsed = time.perf_counter() - started
    finally:
        memory.close()
    assert receipt.episode_id is None
    assert receipt.rejected == ["personal memory timed out"]
    assert elapsed < 2
    assert client.cancelled.wait(1)


def test_read_timeout_is_raised_for_the_file_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang(*_args: object, **_kwargs: object) -> list[object]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr("nwb.memory.graphiti_memory.EntityEdge.get_by_group_ids", _hang)
    memory = _memory(_SlowWrite(), _short_timeouts(recall_ms=4000, io_ms=200))
    actor = Actor("acme", "u1", _ASOF)
    memory._indexed.add(memory._group(actor))
    try:
        started = time.perf_counter()
        with pytest.raises(TimeoutError):
            memory.facts_as_of(actor)
        elapsed = time.perf_counter() - started
    finally:
        memory.close()
    assert elapsed < 2


def test_recall_budget_includes_time_spent_queued() -> None:
    memory = _memory(_SlowSearch(), _short_timeouts(recall_ms=250, io_ms=5000))
    actor = Actor("acme", "u1", _ASOF)
    memory._indexed.add(memory._group(actor))
    entered = threading.Event()

    def _block() -> None:
        async def _hold() -> None:
            entered.set()
            await asyncio.sleep(2.5)

        memory._run(_hold(), timeout=5)

    worker = threading.Thread(target=_block)
    worker.start()
    assert entered.wait(2)
    try:
        started = time.perf_counter()
        result = memory.recall(actor, "payments team", {"u1"}, 3)
        elapsed = time.perf_counter() - started
    finally:
        worker.join(timeout=5)
        memory.close()
    assert result.hits == []
    assert result.skipped is True
    assert result.not_searched == ["graphiti"]
    assert "latency budget" in result.note
    assert elapsed < 1.2
