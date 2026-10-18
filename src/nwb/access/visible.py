"""AccessPolicy adapters. Same rule, two enforcement points: Postgres RLS (nw_can_read) and the SQLite CTE.

Both must pass the identical access-matrix contract tests (tests/contract/test_access_matrix.py).

A company fact is visible when its source document or record is. That allow-list is
``visible_document_ids`` / ``visible_record_ids``. On Postgres those reads are row-level security
from migration 0004.
"""

from __future__ import annotations

from datetime import date
from importlib import resources

from nwb.ports import AccessPolicy, Dialect, SqlDatabase

_TEMPLATE = resources.files("nwb.access").joinpath("sql/visible.sqlite.sql").read_text()
_SQLITE_RECORDS = _TEMPLATE.format(table="records", rtype="record", extra="")
_SQLITE_DOCUMENTS = _TEMPLATE.format(table="documents", rtype="document", extra="AND r.deleted_at IS NULL")


class SqliteAccessPolicy:
    """App-level filter: every repository read must go through these CTEs."""

    def __init__(self, db: SqlDatabase) -> None:
        if db.dialect is not Dialect.SQLITE:
            raise TypeError("SqliteAccessPolicy needs a SQLite database")
        self._db = db

    def _ids(self, sql: str, person_id: str, asof: date) -> list[str]:
        with self._db.unit_of_work() as uow:
            rows = uow.fetchall(sql, {"me": person_id, "asof": asof.isoformat()})
        return [str(r["id"]) for r in rows]

    def visible_record_ids(self, person_id: str, asof: date) -> list[str]:
        return self._ids(_SQLITE_RECORDS, person_id, asof)

    def visible_document_ids(self, person_id: str, asof: date) -> list[str]:
        return self._ids(_SQLITE_DOCUMENTS, person_id, asof)

    def can_read_record(self, person_id: str, asof: date, record_id: str) -> bool:
        return record_id in set(self.visible_record_ids(person_id, asof))


class PostgresRlsAccessPolicy:
    """Database-enforced: reads run as role nw_app with app.person_id/app.asof set for the transaction."""

    def __init__(self, db: SqlDatabase) -> None:
        if db.dialect is not Dialect.POSTGRES:
            raise TypeError("PostgresRlsAccessPolicy needs a Postgres database")
        self._db = db

    def _ids(self, table: str, person_id: str, asof: date) -> list[str]:
        if table not in {"records", "documents"}:
            raise ValueError(table)
        with self._db.unit_of_work(as_person=person_id, asof=asof) as uow:
            rows = uow.fetchall(f"SELECT id FROM {table} ORDER BY id")
        return [str(r["id"]) for r in rows]

    def visible_record_ids(self, person_id: str, asof: date) -> list[str]:
        return self._ids("records", person_id, asof)

    def visible_document_ids(self, person_id: str, asof: date) -> list[str]:
        return self._ids("documents", person_id, asof)

    def can_read_record(self, person_id: str, asof: date, record_id: str) -> bool:
        with self._db.unit_of_work(as_person=person_id, asof=asof) as uow:
            return uow.fetchone("SELECT 1 AS ok FROM records WHERE id = :id", {"id": record_id}) is not None


def access_policy_for(db: SqlDatabase) -> AccessPolicy:
    return PostgresRlsAccessPolicy(db) if db.dialect is Dialect.POSTGRES else SqliteAccessPolicy(db)
