"""Copy the SQL ``facts`` table into the per-level FalkorDB graphs before migration 0019 drops it.

Each row becomes the same fact node B2 writes: ``valid_at`` / ``invalid_at``, status, authority, scope, unit,
label, quote, source title and the supersedes / overrides / related ids. The level is the classification of
the fact's source document or record, or the unknown-tag level when the source is gone (the loader's rule).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from nwb.errors import MigrationError
from nwb.ports import Row, SqlDatabase
from nwb.temporal.facts import Authority, Fact
from nwb.temporal.graph_facts import GraphFactBook

_SELECT = (
    "SELECT f.id, f.company_id, f.key, f.about, f.scope, f.value, f.unit, f.valid_from, f.valid_to, f.authority, "
    "f.status, f.src_type, f.src_id, f.quote, f.supersedes_id, f.overrides_id, f.related_id, f.label, f.created_at, "
    "COALESCE(d.classification, r.classification) AS level, d.title AS title "
    "FROM facts f "
    "LEFT JOIN documents d ON f.src_type = 'document' AND d.id = f.src_id "
    "LEFT JOIN records r ON f.src_type = 'record' AND r.id = f.src_id "
    "ORDER BY f.id"
)


class SqlFactsToGraph:
    """``TableCopy`` for ``facts``. The graph is opened only when there is something to copy."""

    table = "facts"

    def __init__(self, book: Callable[[], GraphFactBook], fallback_level: str) -> None:
        self._book = book
        self._fallback = fallback_level

    def copy(self, db: SqlDatabase) -> int:
        with db.unit_of_work() as uow:
            rows = uow.fetchall(_SELECT)
        placed = [(_fact(row), str(row["level"] or self._fallback)) for row in rows]
        book = self._book()
        # Edges are added only when both nodes exist, so write every node, then write again for the edges.
        for _ in range(2):
            for fact, level in placed:
                book.save(fact, level)
        missing = [fact.id for fact, _ in placed if book.get(fact.id) is None]
        if missing:
            raise MigrationError(f"{len(missing)} facts did not reach the graphs, e.g. {missing[0]}")
        return len(placed)


def _fact(row: Row) -> Fact:
    valid_from = _day(row["valid_from"])
    if valid_from is None:
        raise MigrationError(f"fact {row['id']} has no valid_from")
    status = str(row["status"])
    fact = Fact(
        key=str(row["key"]),
        about=str(row["about"]),
        value=str(row["value"]),
        valid_from=valid_from,
        authority=Authority(int(row["authority"])),
        src_type="record" if row["src_type"] == "record" else "document",
        src_id=str(row["src_id"]),
        quote=str(row["quote"]),
        company_id=str(row["company_id"]),
        scope=row["scope"],
        unit=row["unit"],
        draft=status == "draft",
        valid_to=_day(row["valid_to"]),
        status=status,  # type: ignore[arg-type]
        supersedes_id=row["supersedes_id"],
        overrides_id=row["overrides_id"],
        related_id=row["related_id"],
        label=row["label"],
        title=row["title"],
        recorded_at=_epoch_ms(row["created_at"]),
    )
    if fact.id != row["id"]:
        # Node ids are derived from the fact; links between facts point at those ids.
        raise MigrationError(f"fact {row['id']} does not match its derived id {fact.id}; nothing dropped")
    return fact


def _day(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value if not isinstance(value, datetime) else value.date()
    return date.fromisoformat(str(value)[:10])


def _epoch_ms(value: object) -> int | None:
    if value is None:
        return None
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return int(stamp.timestamp() * 1000)
