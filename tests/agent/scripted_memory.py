"""In-memory stand-in for Graphiti personal memory. No FalkorDB and no LLM.

Team moves are generic: a later "moved to the X team" closes the current fact about a different team
by setting ``invalid_at``. Tests use this so the agent suite stays hermetic.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.errors import NotFoundError
from nwb.identity import Actor
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

_TEAM = re.compile(
    r"\b(?:work(?:s|ing)? on|moved to|moving to) the ([A-Za-z][A-Za-z0-9]+) (team|office)\b",
    re.IGNORECASE,
)
_NAMED = re.compile(r"\bthe ([A-Za-z][A-Za-z0-9]+) (?:team|office)\b", re.IGNORECASE)
_PREFERENCE = re.compile(r"\b(?:prefer|prefers|preferred)\b", re.IGNORECASE)
_MISSING = "fact not found"


class ScriptedPersonalMemory:
    def __init__(self, graph_cfg: GraphConfig, memory_cfg: MemoryConfig, guards: Sequence[WriteGuard]) -> None:
        self._graph = graph_cfg
        self._cfg = memory_cfg
        self._guards = list(guards)
        self._facts: dict[str, list[DatedFact]] = {}
        self.last_known_entities: list[str] = []
        self.recall_delay_s = 0.0

    def close(self) -> None:
        return None

    def remember_chat(
        self,
        actor: Actor,
        messages: Sequence[tuple[str, str]],
        known_entities: Sequence[str],
        reference_time: datetime,
    ) -> EpisodeReceipt:
        body, rejected = screen_user_text(self._guards, messages, self._cfg.facts.chunk_chars)
        self.last_known_entities = list(known_entities)[: self._cfg.facts.known_entity_limit]
        if not body:
            return EpisodeReceipt(None, self.last_known_entities, rejected, 0)
        fact_ids = self._ingest(actor, body, reference_time)
        return EpisodeReceipt(uuid4().hex, self.last_known_entities, rejected, len(fact_ids), fact_ids)

    def append_fact(
        self, actor: Actor, content: str, source_text: str | None, reference_time: datetime
    ) -> EpisodeReceipt:
        reason = rejection(self._guards, content, source_text)
        if reason:
            return EpisodeReceipt(None, [], [reason], 0)
        fact_ids = self._ingest(actor, content, reference_time)
        return EpisodeReceipt(uuid4().hex, [], [], len(fact_ids), fact_ids)

    def recall(self, actor: Actor, query: str, audience: set[str], limit: int) -> Recall:
        started = time.perf_counter()
        if audience_blocked(actor, audience):
            note = "not searched: graphiti (the reply is not only for the reader)"
            return Recall(query, [], False, ["graphiti"], "empty", note, 0)
        budget = self._cfg.facts.latency_budget_ms / 1000

        def work() -> list[RecallHit]:
            if self.recall_delay_s:
                time.sleep(self.recall_delay_s)
            return self._rank(actor, query)

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(work)
            try:
                ranked = fut.result(timeout=budget)
            except FuturesTimeout:
                note = "not searched: graphiti (over the latency budget)"
                return Recall(query, [], True, ["graphiti"], "skipped", note, _ms(started))
        hits = apply_slots(ranked, self._cfg.facts.slots, self._cfg.facts.preference_kinds)[:limit]
        note = "searched graphiti" if hits else "searched graphiti; nothing current matched"
        return Recall(query, hits, False, [], "ok" if hits else "empty", note, _ms(started))

    def read_fact(self, actor: Actor, fact_id: str) -> DatedFact:
        found = next((f for f in self._all(actor) if f.id == fact_id), None)
        if found is None:
            raise NotFoundError(_MISSING)
        return found

    def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]:
        facts = self._all(actor)
        if include_history:
            return facts
        return [f for f in facts if f.current_on(actor.asof)]

    def known_entities(self, actor: Actor) -> list[str]:
        names: dict[str, None] = {}
        for fact in self._facts.get(self._group(actor), []):
            for match in _NAMED.finditer(fact.text):
                names.setdefault(match.group(1), None)
        return list(names)

    def _all(self, actor: Actor) -> list[DatedFact]:
        return link_supersession(self._facts.get(self._group(actor), []))

    def _rank(self, actor: Actor, query: str) -> list[RecallHit]:
        terms = self._cfg.search.terms(query)
        hits: list[RecallHit] = []
        for fact in self.facts_as_of(actor):
            if fact.kind in self._cfg.facts.preference_kinds:
                continue
            low = fact.text.lower()
            if terms and not any(term in low for term in terms):
                continue
            score = 0.8 if not terms else round(sum(term in low for term in terms) / len(terms), 4)
            hits.append(RecallHit(fact, score))
        hits.sort(key=lambda h: (-h.score, h.fact.id))
        return hits

    def _ingest(self, actor: Actor, body: str, when: datetime) -> list[str]:
        group = self._group(actor)
        bucket = self._facts.setdefault(group, [])
        fact_ids: list[str] = []
        for line in body.splitlines():
            text = " ".join(line.split())
            if len(text) < 3:
                continue
            team = _TEAM.search(text)
            if team:
                name = team.group(1)
                self._close_other_teams(bucket, name, when)
                relation = "WORKS_ON" if re.search(r"\bworks?\b", text, re.IGNORECASE) else "MOVED_TO"
                fact = _new(text, "relationship", relation, when)
            elif _PREFERENCE.search(text):
                fact = _new(text, "preference", "PREFERS", when)
            else:
                fact = _new(text, self._cfg.facts.default_kind, "ABOUT", when)
            bucket.append(fact)
            fact_ids.append(fact.id)
        return fact_ids

    def _close_other_teams(self, facts: list[DatedFact], team: str, when: datetime) -> None:
        for i, fact in enumerate(facts):
            if fact.invalid_at is not None or fact.kind != "relationship":
                continue
            named = _NAMED.search(fact.text)
            if named and named.group(1).casefold() != team.casefold():
                facts[i] = replace(fact, invalid_at=when)

    def _group(self, actor: Actor) -> str:
        return self._graph.user_group(actor.company_id, actor.person_id)


def _new(text: str, kind: str, name: str, when: datetime) -> DatedFact:
    return DatedFact(id=uuid4().hex, text=text, kind=kind, name=name, valid_at=when, invalid_at=None)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
