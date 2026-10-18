"""Copy before drop: a migration that drops a SQL table runs only after its rows were copied to their new home.

``nwb migrate`` runs the copy registered for a migration just before applying it, checks that every row
arrived, and records ``copied:<table>:<rows>`` in ``schema_migrations``. The migration's SQL refuses to drop
unless the table is empty or that exact marker exists. A runner without the copy step, or a copy that stopped
part way, leaves the table in place and the migration failed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from nwb.errors import MigrationError
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.ports import Dialect, SqlDatabase

log = get_logger(__name__)
_TABLE = re.compile(r"^[a-z_][a-z0-9_]*$")


class TableCopy(Protocol):
    """Copies every row of one SQL table somewhere else and returns how many rows it wrote."""

    table: str

    def copy(self, db: SqlDatabase) -> int:
        """Rows carried over, plus rows the copy deliberately leaves behind. Anything else means not finished."""
        ...


def copy_marker(table: str, rows: int) -> str:
    return f"copied:{table}:{rows}"


def record_marker(db: SqlDatabase, name: str) -> None:
    """Insert ``name`` into ``schema_migrations`` once. A rerun after a failed drop finds it already there."""
    with db.unit_of_work() as uow:
        uow.execute(
            "INSERT INTO schema_migrations (name) SELECT CAST(:n AS TEXT) WHERE NOT EXISTS "
            "(SELECT 1 FROM schema_migrations WHERE name = :n)",
            {"n": name},
        )


class RowLedger:
    """Per-row progress for a copy whose target is not idempotent, so a rerun resumes instead of duplicating."""

    def __init__(self, db: SqlDatabase, table: str) -> None:
        self._db = db
        self._prefix = f"copied:{table}:row:"

    def done(self) -> set[str]:
        with self._db.unit_of_work() as uow:
            rows = uow.fetchall("SELECT name FROM schema_migrations WHERE name LIKE :p", {"p": self._prefix + "%"})
        return {str(r["name"])[len(self._prefix) :] for r in rows if str(r["name"]).startswith(self._prefix)}

    def mark(self, row_id: str) -> None:
        record_marker(self._db, self._prefix + row_id)


class CopyBeforeDrop:
    """``Migrator`` hook: run the copies registered under a migration's stem (``0019_drop_facts``), in order."""

    def __init__(self, db: SqlDatabase, copies: Mapping[str, Sequence[Callable[[], TableCopy]]]) -> None:
        self._db = db
        self._copies = copies

    def __call__(self, migration_name: str) -> None:
        for build in self._copies.get(migration_name.split(".", 1)[0], ()):
            self._copy(migration_name, build())

    def _copy(self, migration_name: str, copy: TableCopy) -> None:
        total = self._rows(copy.table)
        if not total:
            return
        written = copy.copy(self._db)
        if written != total:
            raise MigrationError(f"{migration_name}: copied {written} of {total} {copy.table} rows; nothing dropped")
        record_marker(self._db, copy_marker(copy.table, total))
        dataflow(Hop.MIGRATE, migration=migration_name, copied=copy.table, rows=total)
        log.info("copied rows before drop", migration=migration_name, table=copy.table, rows=total)

    def _rows(self, table: str) -> int | None:
        """Row count, or None when the table is already gone."""
        if not _TABLE.match(table):
            raise MigrationError(f"copy table must be a plain identifier, got {table!r}")
        with self._db.unit_of_work() as uow:
            if self._db.dialect is Dialect.POSTGRES:
                present = uow.fetchone("SELECT to_regclass(:t) IS NOT NULL AS ok", {"t": f"public.{table}"})
                exists = bool(present and present["ok"])
            else:
                exists = (
                    uow.fetchone("SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = :t", {"t": table})
                    is not None
                )
            if not exists:
                return None
            row = uow.fetchone(f"SELECT count(*) AS n FROM {table}")
        return int(row["n"]) if row else 0
