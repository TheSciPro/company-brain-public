"""Transactional outbox: written in the same transaction as the SQL change; drained by jobs.graph_sync."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from nwb.ports import Row, UnitOfWork

Op = Literal["upsert", "close", "hide"]


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    id: int
    aggregate: str
    aggregate_id: str
    op: str
    payload: dict[str, Any]


class OutboxRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def add(self, aggregate: str, aggregate_id: str, payload: dict[str, Any], op: Op = "upsert") -> None:
        self._uow.execute(
            "INSERT INTO graph_outbox (aggregate, aggregate_id, op, payload) VALUES (:a, :i, :o, :p)",
            {
                "a": aggregate,
                "i": aggregate_id,
                "o": op,
                "p": json.dumps(payload, default=str, sort_keys=True),
            },
        )

    def add_many(self, aggregate: str, items: list[tuple[str, dict[str, Any]]], op: Op = "upsert") -> None:
        self._uow.executemany(
            "INSERT INTO graph_outbox (aggregate, aggregate_id, op, payload) VALUES (:a, :i, :o, :p)",
            [{"a": aggregate, "i": i, "o": op, "p": json.dumps(p, default=str, sort_keys=True)} for i, p in items],
        )

    def pending(self, limit: int) -> list[OutboxEntry]:
        rows = self._uow.fetchall(
            "SELECT id, aggregate, aggregate_id, op, payload FROM graph_outbox "
            "WHERE processed_at IS NULL ORDER BY id LIMIT :n",
            {"n": limit},
        )
        return [_entry(r) for r in rows]

    def mark_processed(self, ids: list[int]) -> None:
        self._uow.executemany(
            "UPDATE graph_outbox SET processed_at = CURRENT_TIMESTAMP, error = NULL WHERE id = :id",
            [{"id": i} for i in ids],
        )

    def mark_failed(self, ids: list[int], error: str) -> None:
        self._uow.executemany(
            "UPDATE graph_outbox SET attempts = attempts + 1, error = :e WHERE id = :id",
            [{"id": i, "e": error[:2000]} for i in ids],
        )

    def count_pending(self) -> int:
        row = self._uow.fetchone("SELECT count(*) AS n FROM graph_outbox WHERE processed_at IS NULL")
        return int(row["n"]) if row else 0


def _entry(r: Row) -> OutboxEntry:
    return OutboxEntry(
        int(r["id"]), str(r["aggregate"]), str(r["aggregate_id"]), str(r["op"]), json.loads(r["payload"])
    )
