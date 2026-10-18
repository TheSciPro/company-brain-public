"""ContentRepository: single writer for records, documents and their grants (+ outbox in the same transaction).

Idempotent by content hash: an unchanged item is skipped entirely (no SQL write, no outbox row).
"""

from __future__ import annotations

from collections.abc import Sequence

from nwb.db.models import Document, Grant, Record
from nwb.db.outbox import OutboxRepository
from nwb.ports import UnitOfWork

_RECORD_COLS = (
    "id, company_id, source, source_id, record_type, owner_person_id, owner_unit_id, dept_id, classification, "
    "share_scope, source_tag, personal, payload, hash, occurred_at"
)
_DOC_COLS = (
    "id, company_id, title, doc_type, version, owner_person_id, owner_unit_id, dept_id, classification, "
    "share_scope, source_tag, personal, blob_url, mime, bytes, hash, source, source_id"
)


def _upsert_sql(table: str, cols: str) -> str:
    names = [c.strip() for c in cols.split(",")]
    sets = ", ".join(f"{c} = excluded.{c}" for c in names if c != "id")
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({', '.join(':' + c for c in names)}) "
        f"ON CONFLICT (id) DO UPDATE SET {sets}"
    )


class ContentRepository:
    def __init__(self, uow: UnitOfWork, outbox: OutboxRepository | None = None) -> None:
        self._uow = uow
        self._outbox = outbox or OutboxRepository(uow)

    def _hashes(self, table: str) -> dict[str, str]:
        return {str(r["id"]): str(r["hash"]) for r in self._uow.fetchall(f"SELECT id, hash FROM {table}")}

    def upsert_records(self, items: Sequence[tuple[Record, Sequence[Grant]]]) -> tuple[int, int]:
        """Returns (written, skipped)."""
        known = self._hashes("records")
        changed = [(r, g) for r, g in items if known.get(r.id) != r.hash]
        if changed:
            self._uow.executemany(_upsert_sql("records", _RECORD_COLS), [r.model_dump() for r, _ in changed])
            self._replace_grants("record", changed)
            self._outbox.add_many(
                "record", [(r.id, r.model_dump(mode="json", exclude={"payload"})) for r, _ in changed]
            )
        return len(changed), len(items) - len(changed)

    def upsert_documents(self, items: Sequence[tuple[Document, Sequence[Grant]]]) -> tuple[int, int]:
        known = self._hashes("documents")
        changed = [(d, g) for d, g in items if known.get(d.id) != d.hash]
        if changed:
            self._uow.executemany(_upsert_sql("documents", _DOC_COLS), [d.model_dump() for d, _ in changed])
            self._replace_grants("document", changed)
            self._outbox.add_many("document", [(d.id, d.model_dump(mode="json")) for d, _ in changed])
        return len(changed), len(items) - len(changed)

    def _replace_grants(self, rtype: str, items: Sequence[tuple[Record | Document, Sequence[Grant]]]) -> None:
        self._uow.executemany(
            "DELETE FROM grants WHERE resource_type = :t AND resource_id = :r",
            [{"t": rtype, "r": c.id} for c, _ in items],
        )
        grants = [g for _, gs in items for g in gs]
        self._uow.executemany(
            "INSERT INTO grants (id, company_id, subject_type, subject_id, resource_type, resource_id, relation, "
            "valid_from, valid_to, reason) VALUES (:id, :company_id, :subject_type, :subject_id, :resource_type, "
            ":resource_id, :relation, :valid_from, :valid_to, 'acl')",
            [{**g.model_dump(), "id": g.id, "company_id": c.company_id} for c, gs in items for g in gs],
        )
        self._outbox.add_many("grant", [(g.id, {**g.model_dump(mode="json"), "id": g.id}) for g in grants])

    def log(self, source: str, item_id: str | None, status: str, reason: str | None = None) -> None:
        self._uow.execute(
            "INSERT INTO ingest_log (source, item_id, status, reason) VALUES (:s, :i, :st, :r)",
            {"s": source, "i": item_id, "st": status, "r": reason},
        )
