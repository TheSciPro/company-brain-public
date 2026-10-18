"""Supersession on write and as-of reads. Recency alone never wins; authority does.

The rules run here. The facts they read and write live in the per-level FalkorDB graphs
(``GraphFactBook``), not in SQL.

- draft sources never become current (status 'draft'; linked to what they contradict);
- no current fact -> current;
- same value -> 'corroborates' (extra evidence only);
- authority >= current -> close the current fact the day before (``invalid_at``), and point at it;
- lower authority -> 'conflict' plus a conflict alert;
- a scoped fact links OVERRIDES to the general fact.

A closed fact stays in the graph. An as-of question about the old date still returns it, and the
answer says it was later withdrawn.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta

from nwb.errors import NwbError
from nwb.temporal.facts import Fact, FactAnswer
from nwb.temporal.graph_facts import GraphFactBook

_MARKUP = re.compile(r"[*_`#>|\\]+")
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"})
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|\u2026)\s*")
_MAX_GAP = 400  # an elided quote must stay local: each part within this many characters of the previous one


class QuoteMismatchError(NwbError):
    """A fact's quote does not appear in its source text."""


def _plain(text: str) -> str:
    """Compare quotes as a reader would: no markdown emphasis, one hyphen, case-folded, whitespace collapsed."""
    return " ".join(_MARKUP.sub("", text).translate(_DASHES).casefold().split())


def check_quote(quote: str, source_text: str) -> None:
    """The quote must appear in the source. Markdown emphasis, dash style and case are ignored, words are not.
    An explicit ellipsis marks an elision: every part must appear, in order, close together."""
    parts = [p.strip(" .;:,\"'") for p in _ELLIPSIS.split(_plain(quote))]
    src = _plain(source_text)
    if not parts or any(len(p) < 3 for p in parts) or not _in_order(parts, src):
        raise QuoteMismatchError(f"quote not found in source: {quote[:80]!r}")


def _in_order(parts: list[str], src: str) -> bool:
    start = src.find(parts[0])
    while start >= 0:
        pos, ok = start + len(parts[0]), True
        for p in parts[1:]:
            nxt = src.find(p, pos)
            if nxt < 0 or nxt - pos > _MAX_GAP:
                ok = False
                break
            pos = nxt + len(p)
        if ok:
            return True
        start = src.find(parts[0], start + 1)
    return False


def _now() -> int:
    return time.time_ns() // 1_000_000


class PrecedenceEngine:
    def __init__(self, book: GraphFactBook) -> None:
        self._book = book

    def on_new_fact(self, fact: Fact, level: str) -> Fact:
        existing = self._book.get(fact.id)
        if existing is not None:
            self._finish_supersession(existing)  # a replay completes a close that failed last time
            return existing  # idempotent replay
        fact = fact.with_(recorded_at=fact.recorded_at if fact.recorded_at is not None else _now(), level=level)
        current = self._book.valid_at(fact.key, fact.about, fact.scope, fact.valid_from, ("current",))
        cur = current[0] if current else None
        if fact.scope is not None:
            general = self._book.valid_at(fact.key, fact.about, None, fact.valid_from, ("current",))
            if general:
                fact = fact.with_(overrides_id=general[0].id)

        if fact.draft:
            stored = fact.with_(status="draft", related_id=cur.id if cur else None)
        elif cur is None:
            stored = fact.with_(status="current")
        elif cur.value == fact.value:
            stored = fact.with_(status="corroborates", related_id=cur.id, valid_to=fact.valid_from)
        elif fact.authority >= cur.authority:
            # The two nodes can live in different graphs, so this is two writes. The new node goes first: if the
            # close then fails, both are current for a moment and as-of still prefers the newer one. Never none.
            stored = fact.with_(status="current", supersedes_id=cur.id)
            self._book.save(stored, level)
            self._book.close(cur, _closing_day(cur, stored))
            return stored
        else:
            stored = fact.with_(status="conflict", related_id=cur.id, valid_to=fact.valid_from)
            self._book.save(stored, level)
            self._book.add_alert(
                "conflict", stored, cur, f"{fact.src_id} says {fact.value}; {cur.src_id} says {cur.value}"
            )
            return stored
        self._book.save(stored, level)
        return stored

    def _finish_supersession(self, fact: Fact) -> None:
        if fact.status != "current" or not fact.supersedes_id:
            return
        old = self._book.get(fact.supersedes_id)
        if old is not None and old.status == "current":
            self._book.close(old, _closing_day(old, fact))

    def as_of(
        self,
        key: str,
        about: str,
        asof: date,
        *,
        scope: str | None = None,
        src_ids: set[str] | None = None,
    ) -> FactAnswer:
        """Best visible fact valid at ``asof``. Scoped facts beat general ones; drafts and conflicts are reported."""
        live = self._book.live_statuses

        def best(wanted: str | None) -> Fact | None:
            rows = self._book.valid_at(key, about, wanted, asof, live, src_ids)
            return rows[0] if rows else None

        general = best(None)
        scoped = best(scope) if scope is not None else None
        winner = scoped or general
        replaced = self._book.get(winner.supersedes_id) if winner and winner.supersedes_id else None
        if replaced is not None and src_ids is not None and replaced.src_id not in src_ids:
            replaced = None
        contested = tuple(c for c in self._book.contested(key, about, scope, src_ids) if c.valid_from <= asof)
        return FactAnswer(
            fact=winner,
            overridden=general if scoped is not None else None,
            replaced=replaced,
            contested=contested,
        )


def _closing_day(old: Fact, new: Fact) -> date:
    """The superseded fact ends the day before the new one starts (never before it began)."""
    return max(old.valid_from, new.valid_from - timedelta(days=1))
