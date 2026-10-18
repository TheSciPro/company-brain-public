"""Graphiti personal memory on FalkorDB. One graph per ``group_id`` ``{company}__{user}``.

Writes are append-only: a newer fact sets ``invalid_at`` on the one it replaces. This module never issues
``DELETE`` or ``DETACH DELETE``. Reads filter by the reader's group and by ``valid_at`` / ``invalid_at`` at
the ask date. Search that exceeds ``facts.latency_budget_ms`` returns nothing and says ``graphiti`` was
not searched. That budget includes time spent waiting for the shared loop. Writes and reads use
``facts.io_timeout_ms``; on timeout the call is cancelled and the method returns a degraded result.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import GroupsEdgesNotFoundError
from graphiti_core.graph_queries import get_fulltext_indices, get_range_indices
from graphiti_core.nodes import EntityNode, EpisodeType
from pydantic import BaseModel

from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.errors import NotFoundError
from nwb.identity import Actor
from nwb.kg.embedding import EmbeddingReranker, GraphitiEmbedder
from nwb.kg.server import FalkorEndpoint
from nwb.memory.embeddings import Embedder
from nwb.memory.guards import WriteGuard
from nwb.memory.store import (
    DatedFact,
    EpisodeReceipt,
    Recall,
    RecallHit,
    apply_slots,
    audience_blocked,
    link_supersession,
    rejection,
    screen_user_text,
)
from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)

_MISSING = "fact not found"
_TIMED_OUT = "personal memory timed out"


def open_personal_memory(
    endpoint: FalkorEndpoint,
    embedder: Embedder,
    llm_client: Any,
    graph_cfg: GraphConfig,
    memory_cfg: MemoryConfig,
    guards: Sequence[WriteGuard],
) -> GraphitiPersonalMemory:
    """Build a Graphiti client on ``endpoint``. The caller owns the Falkor process, if it started one."""
    _quiet_falkor_init()
    driver = FalkorDriver(
        host=endpoint.host,
        port=endpoint.port,
        username=endpoint.username,
        password=endpoint.password,
        database="nwb_personal",
    )
    client = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=GraphitiEmbedder(embedder),
        cross_encoder=EmbeddingReranker(embedder),
        max_coroutines=graph_cfg.personal.semaphore_limit,
    )
    return GraphitiPersonalMemory(client, graph_cfg, memory_cfg, guards)


class GraphitiPersonalMemory:
    def __init__(
        self, client: Any, graph_cfg: GraphConfig, memory_cfg: MemoryConfig, guards: Sequence[WriteGuard]
    ) -> None:
        self._client = client
        self._graph = graph_cfg
        self._cfg = memory_cfg
        self._guards = list(guards)
        self._indexed: set[str] = set()
        self._entity_types = _entity_models(graph_cfg.personal.entity_types)
        self._closed = False
        # FalkorDB's async client is bound to the loop that first uses it. One loop thread for this
        # object: chat and consolidation can run on different threads, and run_until_complete is
        # not safe to call on one loop from two of them.
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="nwb-graphiti-memory", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _run(self, coro: Any, *, timeout: float) -> Any:
        """Run ``coro`` on the memory loop. ``timeout`` covers queue wait and the call itself.

        On timeout the future is cancelled and ``TimeoutError`` is raised. Callers turn that into
        a degraded result; they do not wait for the cancelled call to finish.
        """
        if self._closed or not self._ready.wait(timeout=5) or self._loop.is_closed():
            coro.close()
            raise RuntimeError("personal memory is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Finished in the same instant as the deadline: keep the real result.
            if not future.cancel() and future.done() and not future.cancelled():
                return future.result()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._ready.wait(timeout=5):
            return
        try:
            closing = self._client.close()
            if asyncio.iscoroutine(closing):
                asyncio.run_coroutine_threadsafe(closing, self._loop).result(timeout=5)
        except Exception:
            log.warning("personal memory close failed", exc_info=True)
        finally:
            if not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    def remember_chat(
        self,
        actor: Actor,
        messages: Sequence[tuple[str, str]],
        known_entities: Sequence[str],
        reference_time: datetime,
    ) -> EpisodeReceipt:
        body, rejected = screen_user_text(self._guards, messages, self._cfg.facts.chunk_chars)
        names = _cap_names(known_entities, self._cfg.facts.known_entity_limit)
        if not body:
            dataflow(Hop.MEMORY_WRITE, event="REJECTED", person=actor.person_id, rejected=len(rejected))
            return EpisodeReceipt(None, list(names), rejected, 0)
        try:
            return self._run(
                self._add(actor, body, names, reference_time, rejected, source="remember_chat"),
                timeout=self._io_timeout(),
            )
        except TimeoutError:
            log.warning("personal memory write timed out", person=actor.person_id)
            return EpisodeReceipt(None, list(names), [*rejected, _TIMED_OUT], 0)

    def append_fact(
        self, actor: Actor, content: str, source_text: str | None, reference_time: datetime
    ) -> EpisodeReceipt:
        reason = rejection(self._guards, content, source_text)
        if reason:
            dataflow(Hop.MEMORY_WRITE, event="REJECTED", person=actor.person_id, reason=reason)
            return EpisodeReceipt(None, [], [reason], 0)
        try:
            return self._run(
                self._add(actor, content, [], reference_time, [], source="fact_file"),
                timeout=self._io_timeout(),
            )
        except TimeoutError:
            log.warning("personal memory write timed out", person=actor.person_id)
            return EpisodeReceipt(None, [], [_TIMED_OUT], 0)

    def recall(self, actor: Actor, query: str, audience: set[str], limit: int) -> Recall:
        started = _now_ms()
        if audience_blocked(actor, audience):
            note = "not searched: graphiti (the reply is not only for the reader)"
            dataflow(
                Hop.RETRIEVAL,
                kind="memory_recall",
                person=actor.person_id,
                skipped=False,
                honesty="empty",
                note=note,
            )
            return Recall(query, [], False, ["graphiti"], "empty", note, 0)
        group_id = self._group(actor)
        budget = self._cfg.facts.latency_budget_ms / 1000

        async def _search() -> list[Any]:
            await self._ensure_indices(group_id)
            _gid, driver, _clients = self._client._resolve_request_scope(group_id)
            return await self._client.search(
                query,
                group_ids=[group_id],
                num_results=max(limit * 4, self._graph.personal.search_limit),
                driver=driver,
            )

        try:
            # The budget starts when the call is submitted, so time queued behind another
            # Graphiti call counts. An inner wait_for would start only after the coro runs.
            edges = self._run(_search(), timeout=budget)
        except TimeoutError:
            note = "not searched: graphiti (over the latency budget)"
            dataflow(
                Hop.RETRIEVAL,
                kind="memory_recall",
                person=actor.person_id,
                skipped=True,
                ms=_now_ms() - started,
                honesty="skipped",
                not_searched=["graphiti"],
            )
            return Recall(query, [], True, ["graphiti"], "skipped", note, _now_ms() - started)
        except Exception as exc:
            log.warning("memory recall failed", person=actor.person_id, error=type(exc).__name__, exc_info=exc)
            dataflow(
                Hop.RETRIEVAL,
                kind="memory_recall",
                person=actor.person_id,
                skipped=True,
                ms=_now_ms() - started,
                honesty="empty",
                not_searched=["graphiti"],
                error=type(exc).__name__,
            )
            note = "not searched: graphiti (recall failed)"
            return Recall(query, [], True, ["graphiti"], "empty", note, _now_ms() - started)
        hits = self._hits(edges, actor, limit)
        note = "searched graphiti" if hits else "searched graphiti; nothing current matched"
        dataflow(
            Hop.RETRIEVAL,
            kind="memory_recall",
            person=actor.person_id,
            group=group_id,
            skipped=False,
            ms=_now_ms() - started,
            honesty="ok" if hits else "empty",
            returned=len(hits),
        )
        return Recall(query, hits, False, [], "ok" if hits else "empty", note, _now_ms() - started)

    def read_fact(self, actor: Actor, fact_id: str) -> DatedFact:
        found = next((f for f in self.facts_as_of(actor, include_history=True) if f.id == fact_id), None)
        if found is None:
            raise NotFoundError(_MISSING)
        return found

    def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]:
        group_id = self._group(actor)
        # A timeout is raised, not turned into an empty list: "no facts" would hide an outage.
        # File tools catch it and return an error result.
        edges = self._run(self._edges(group_id), timeout=self._io_timeout())
        facts = link_supersession([self._fact(e) for e in edges])
        if include_history:
            return facts
        return [f for f in facts if f.current_on(actor.asof)]

    def known_entities(self, actor: Actor) -> list[str]:
        group_id = self._group(actor)

        async def _names() -> list[str]:
            await self._ensure_indices(group_id)
            driver = self._client.driver.clone(database=group_id)
            nodes = await EntityNode.get_by_group_ids(driver, [group_id], limit=self._cfg.facts.known_entity_limit)
            return [n.name for n in nodes if getattr(n, "name", None)]

        try:
            return self._run(_names(), timeout=self._io_timeout())
        except Exception:
            log.warning("known entities unavailable", person=actor.person_id)
            return []

    async def _add(
        self,
        actor: Actor,
        body: str,
        names: list[str],
        reference_time: datetime,
        rejected: list[str],
        source: str,
    ) -> EpisodeReceipt:
        group_id = self._group(actor)
        await self._ensure_indices(group_id)
        when = reference_time if reference_time.tzinfo else reference_time.replace(tzinfo=UTC)
        instructions = self._instructions(names)
        before = {e.uuid for e in await self._edges(group_id)}
        result = await self._client.add_episode(
            name=f"{source}:{actor.session_id or actor.person_id}",
            episode_body=body,
            source_description=source,
            reference_time=when,
            source=EpisodeType.message,
            group_id=group_id,
            entity_types=self._entity_types or None,
            custom_extraction_instructions=instructions,
            update_communities=False,
        )
        after = await self._edges(group_id)
        episode_id = str(result.episode.uuid)
        fact_ids = _fact_ids(episode_id, getattr(result, "edges", None) or [], after, before)
        dataflow(
            Hop.MEMORY_WRITE,
            event="ADD",
            person=actor.person_id,
            group=group_id,
            episode=episode_id,
            facts=len(fact_ids),
            known=len(names),
        )
        return EpisodeReceipt(episode_id, names, rejected, len(fact_ids), fact_ids)

    async def _edges(self, group_id: str) -> list[Any]:
        await self._ensure_indices(group_id)
        driver = self._client.driver.clone(database=group_id)
        try:
            return await EntityEdge.get_by_group_ids(driver, [group_id])
        except GroupsEdgesNotFoundError:
            return []

    async def _ensure_indices(self, group_id: str) -> None:
        if group_id in self._indexed:
            return
        await _safe_indexes(self._client.driver.clone(database=group_id))
        self._indexed.add(group_id)

    def _hits(self, edges: Sequence[Any], actor: Actor, limit: int) -> list[RecallHit]:
        facts = {f.id: f for f in link_supersession([self._fact(e) for e in edges])}
        ranked: list[RecallHit] = []
        for i, edge in enumerate(edges):
            fact = facts.get(edge.uuid)
            if fact is None or not fact.current_on(actor.asof):
                continue
            ranked.append(RecallHit(fact, round(max(0.05, 1.0 - i * 0.05), 4)))
        slotted = apply_slots(ranked, self._cfg.facts.slots, self._cfg.facts.preference_kinds)
        return slotted[:limit]

    def _kind(self, name: str) -> str:
        """Map a Graphiti relation name onto a slot kind.

        Graphiti stores the relation (``PREFERS``, ``PREFERENCE``), not the slot name ``preference``.
        The lookup is case-insensitive so a paraphrased name still hits ``preference_kinds``.
        """
        folded = {key.casefold(): value for key, value in self._cfg.facts.relation_kinds.items()}
        key = " ".join(name.split()).casefold().replace(" ", "_").replace("-", "_")
        return folded.get(key, self._cfg.facts.default_kind)

    def _fact(self, edge: Any) -> DatedFact:
        name = str(getattr(edge, "name", "") or "RELATES_TO")
        kind = self._kind(name)
        return DatedFact(
            id=str(edge.uuid),
            text=str(edge.fact),
            kind=kind,
            name=name,
            valid_at=_as_dt(edge.valid_at),
            invalid_at=_as_dt(edge.invalid_at),
        )

    def _io_timeout(self) -> float:
        return self._cfg.facts.io_timeout_ms / 1000

    def _group(self, actor: Actor) -> str:
        return self._graph.user_group(actor.company_id, actor.person_id)

    def _instructions(self, names: Sequence[str]) -> str:
        base = self._cfg.facts.extraction_instructions.strip()
        if not names:
            return base
        listed = ", ".join(names)
        extra = f"Reuse these existing names when they are the same thing: {listed}."
        return f"{base}\n{extra}" if base else extra


def _entity_models(spec: dict[str, str]) -> dict[str, type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    for name, description in spec.items():
        models[name] = type(name, (BaseModel,), {"__doc__": description})
    return models


def _cap_names(names: Sequence[str], limit: int) -> list[str]:
    seen: dict[str, None] = {}
    for name in names:
        text = " ".join(name.split())
        if text:
            seen.setdefault(text, None)
        if len(seen) >= limit:
            break
    return list(seen)


def _as_dt(value: Any) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _fact_ids(episode_id: str, created: Sequence[Any], after: Sequence[Any], before: set[Any]) -> list[str]:
    """Edge uuids this episode wrote. Prefer the episode link over 'any new edge'."""
    ordered: dict[str, None] = {}
    for edge in created:
        uid = str(getattr(edge, "uuid", "") or "")
        if uid:
            ordered.setdefault(uid, None)
    for edge in after:
        uid = str(getattr(edge, "uuid", "") or "")
        episodes = {str(item) for item in (getattr(edge, "episodes", None) or [])}
        if uid and episode_id in episodes:
            ordered.setdefault(uid, None)
    if ordered:
        return list(ordered)
    seen = {str(item) for item in before}
    for edge in after:
        uid = str(getattr(edge, "uuid", "") or "")
        if uid and uid not in seen:
            ordered.setdefault(uid, None)
    return list(ordered)


_ON_PROPS = re.compile(r"ON \(([^)]+)\)")


def _quiet_falkor_init() -> None:
    """FalkorDriver starts an index build in the background whenever a loop is running.

    That task races the episode write and a multi-property ``CREATE INDEX`` closes the
    FalkorDBLite connection. Indexes are created explicitly in ``_safe_indexes``.
    """
    if getattr(FalkorDriver, "_nwb_quiet_init", False):
        return
    original = FalkorDriver.__init__

    def quiet(self: FalkorDriver, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        task = getattr(self, "_init_task", None)
        if task is not None and not task.done():
            task.cancel()
            self._init_task = None

    FalkorDriver.__init__ = quiet  # type: ignore[method-assign]
    FalkorDriver._nwb_quiet_init = True  # type: ignore[attr-defined]


def _single_property_indexes(query: str) -> list[str]:
    """FalkorDBLite closes the connection on a multi-property range index. One property each."""
    match = _ON_PROPS.search(query)
    if match is None:
        return [query]
    props = [part.strip() for part in match.group(1).split(",") if part.strip()]
    if len(props) <= 1:
        return [query]
    prefix = query[: match.start()]
    return [f"{prefix}ON ({prop})" for prop in props]


async def _safe_indexes(driver: FalkorDriver) -> None:
    queries: list[str] = []
    for query in get_range_indices(driver.provider):
        queries.extend(_single_property_indexes(query))
    queries.extend(get_fulltext_indices(driver.provider))
    for query in queries:
        try:
            await driver.execute_query(query)
        except Exception as exc:
            log.warning("falkor index skipped", error=type(exc).__name__)
