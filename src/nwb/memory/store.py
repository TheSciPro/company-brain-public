"""One memory system for dated facts.

Preferences and directives live in ``/memories/USER.md`` (Deep Agents file tools). Dated facts live in
Graphiti, one FalkorDB graph per ``group_id`` ``{company}__{user}``, with ``valid_at`` / ``invalid_at``.
There is no second SQL memory store.

``Container.write_guards`` (length, secret, quality, quote) screens both fact writes:

- episode ingestion runs every guard on each user-message chunk (``facts.chunk_chars`` stays under
  ``content_max_chars``, so ``LengthGuard`` does not drop a whole chat);
- a single-fact write from ``/memories/facts/`` runs every guard, including ``LengthGuard``, on the
  whole text.

C4 writes ``Prefer:`` lines to ``USER.md`` via ``edit_file``. ``preference_kinds`` are dropped on recall
and are not stored as Graphiti facts. Do not put answer-style preferences in this store.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import ValidationError

from nwb.identity import Actor
from nwb.memory.guards import WriteGuard
from nwb.memory.models import AddMemoryInput

Honesty = Literal["ok", "empty", "skipped"]


@dataclass(frozen=True, slots=True)
class DatedFact:
    """One Graphiti edge, as the reader may see it. History is closed with ``invalid_at``, never deleted."""

    id: str
    text: str
    kind: str
    name: str
    valid_at: datetime | None
    invalid_at: datetime | None
    supersedes: str | None = None
    superseded_by: str | None = None

    def current_on(self, asof: date) -> bool:
        if self.valid_at is not None and self.valid_at.date() > asof:
            return False
        return self.invalid_at is None or self.invalid_at.date() > asof


@dataclass(frozen=True, slots=True)
class RecallHit:
    fact: DatedFact
    score: float


@dataclass(frozen=True, slots=True)
class Recall:
    query: str
    hits: list[RecallHit]
    skipped: bool
    not_searched: list[str]
    honesty: Honesty
    note: str
    ms: int


@dataclass(frozen=True, slots=True)
class EpisodeReceipt:
    """Append-only write. ``known_entities`` is what extraction was told to reuse.

    ``fact_ids`` are the edge uuids this episode wrote. A file write confirms with these ids:
    Graphiti paraphrases the stored text, so the original sentence is not a reliable match.
    """

    episode_id: str | None
    known_entities: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    facts_written: int = 0
    fact_ids: list[str] = field(default_factory=list)


class PersonalMemory(Protocol):
    """Dated facts for one company. Every method takes the reader from the runtime, never from the model."""

    def remember_chat(
        self,
        actor: Actor,
        messages: Sequence[tuple[str, str]],
        known_entities: Sequence[str],
        reference_time: datetime,
    ) -> EpisodeReceipt: ...

    def recall(self, actor: Actor, query: str, audience: set[str], limit: int) -> Recall: ...

    def read_fact(self, actor: Actor, fact_id: str) -> DatedFact: ...

    def facts_as_of(self, actor: Actor, *, include_history: bool = False) -> list[DatedFact]: ...

    def known_entities(self, actor: Actor) -> list[str]: ...

    def append_fact(
        self, actor: Actor, content: str, source_text: str | None, reference_time: datetime
    ) -> EpisodeReceipt: ...

    def close(self) -> None: ...


def rejection(
    guards: Sequence[WriteGuard], content: str, source_text: str | None, *, verbatim: bool = False
) -> str | None:
    """Run every guard. A rejection reason means the text must not be written."""
    try:
        inp = AddMemoryInput(
            content=content,
            kind="fact",
            mode="verbatim" if verbatim else "summary",
            capture_intent="inferred_durable",
        )
    except ValidationError as exc:
        return exc.errors()[0]["msg"] if exc.errors() else "rejected"
    for guard in guards:
        reason = guard.check(inp, source_text)
        if reason:
            return reason
    return None


def chunks(text: str, size: int) -> list[str]:
    """Word windows at most ``size`` characters, so a long chat still passes ``LengthGuard`` per window."""
    words = text.split()
    if not words:
        return []
    out: list[str] = []
    cur = ""
    for word in words:
        nxt = f"{cur} {word}".strip()
        if cur and len(nxt) > size:
            out.append(cur)
            cur = word
        else:
            cur = nxt
    if cur:
        out.append(cur)
    return out


def screen_user_text(
    guards: Sequence[WriteGuard], messages: Sequence[tuple[str, str]], chunk_chars: int
) -> tuple[str, list[str]]:
    """Keep user chunks that pass every guard. Rejected chunks are reasons, not stored text."""
    kept: list[str] = []
    rejected: list[str] = []
    for role, text in messages:
        if role != "user" or not text.strip():
            continue
        for piece in chunks(text, chunk_chars):
            reason = rejection(guards, piece, text)
            if reason:
                rejected.append(reason)
            else:
                kept.append(piece)
    return "\n".join(kept), rejected


def link_supersession(facts: Sequence[DatedFact]) -> list[DatedFact]:
    """Point a closed fact at the fact whose ``valid_at`` is its ``invalid_at``. Nothing is deleted."""
    pending = list(facts)
    newer_for: dict[str, str] = {}
    older_for: dict[str, str] = {}
    for old in pending:
        if old.invalid_at is None:
            continue
        candidates = [
            n
            for n in pending
            if n.id != old.id and n.valid_at is not None and abs((n.valid_at - old.invalid_at).total_seconds()) < 2
        ]
        same_name = [n for n in candidates if n.name == old.name]
        chosen = same_name or candidates
        if len(chosen) == 1:
            newer_for[old.id] = chosen[0].id
            older_for[chosen[0].id] = old.id
    linked: list[DatedFact] = []
    for fact in pending:
        linked.append(
            DatedFact(
                id=fact.id,
                text=fact.text,
                kind=fact.kind,
                name=fact.name,
                valid_at=fact.valid_at,
                invalid_at=fact.invalid_at,
                supersedes=older_for.get(fact.id),
                superseded_by=newer_for.get(fact.id),
            )
        )
    return linked


def apply_slots(hits: Sequence[RecallHit], slots: dict[str, int], preference_kinds: Sequence[str]) -> list[RecallHit]:
    """Drop preference kinds and keep at most ``slots[kind]`` hits, in the order search returned them."""
    blocked = set(preference_kinds)
    kept: list[RecallHit] = []
    used: dict[str, int] = {}
    for hit in hits:
        kind = hit.fact.kind
        if kind in blocked:
            continue
        cap = slots.get(kind, 1)
        if used.get(kind, 0) >= cap:
            continue
        used[kind] = used.get(kind, 0) + 1
        kept.append(hit)
    return kept


def audience_blocked(actor: Actor, audience: set[str]) -> bool:
    """Ankole: recall only when the reply will be seen by the reader alone."""
    return audience != {actor.person_id}
