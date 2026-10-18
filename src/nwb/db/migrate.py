"""Forward-only numbered SQL migrations: ``NNNN_name.sql`` (all), ``NNNN_name.pg.sql`` / ``.sqlite.sql``."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nwb.db.database import PostgresDatabase
from nwb.db.dialect import to_dialect_ddl
from nwb.errors import MigrationError
from nwb.obs.log import Hop, dataflow
from nwb.ports import Dialect, SqlDatabase

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+?(?:\.(pg|sqlite))?\.sql$")
_TAG = {"pg": Dialect.POSTGRES, "sqlite": Dialect.SQLITE}
# CREATE/DROP ROLE is not allowed inside a transaction block. A DO $$ ... CREATE ROLE ... $$; counts too.
_DO_BLOCK = re.compile(r"DO\s+\$\$.*?\$\$\s*;", re.IGNORECASE | re.DOTALL)
_ROLE_STMT = re.compile(r"\b(?:CREATE|DROP)\s+ROLE\b[^;]*;", re.IGNORECASE)
_ROLE_WORD = re.compile(r"\b(?:CREATE|DROP)\s+ROLE\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


class Migrator:
    def __init__(self, db: SqlDatabase, directory: Path, before: Callable[[str], None] | None = None) -> None:
        """``before(name)`` runs ahead of each pending migration, e.g. ``CopyBeforeDrop``. A raise stops the run."""
        self._db = db
        self._dir = directory
        self._before = before

    def discover(self) -> list[Migration]:
        found: list[Migration] = []
        for p in sorted(self._dir.glob("*.sql")):
            m = _NAME.match(p.name)
            if not m:
                raise MigrationError(f"bad migration filename: {p.name}")
            tag = m.group(2)
            if tag is None or _TAG[tag] is self._db.dialect:
                found.append(Migration(m.group(1) + ":" + p.name, p))
        return found

    def applied(self) -> set[str]:
        with self._db.unit_of_work() as uow:
            uow.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            return {r["name"] for r in uow.fetchall("SELECT name FROM schema_migrations")}

    def migrate(self) -> list[str]:
        done = self.applied()
        ran: list[str] = []
        for mig in self.discover():
            if mig.name in done:
                continue
            script = to_dialect_ddl(mig.path.read_text(), self._db.dialect)
            try:
                if self._before is not None:
                    self._before(mig.name)
                if isinstance(self._db, PostgresDatabase):
                    roles, script = split_role_ddl(script)
                    for stmt in roles:
                        self._db.execute_outside_transaction(stmt)
                with self._db.unit_of_work() as uow:
                    if script.strip():
                        uow.executescript(script)
                    uow.execute("INSERT INTO schema_migrations (name) VALUES (:n)", {"n": mig.name})
            except Exception as exc:
                raise MigrationError(f"{mig.name} failed: {exc}") from exc
            dataflow(Hop.MIGRATE, migration=mig.name, dialect=self._db.dialect)
            ran.append(mig.name)
        return ran


def split_role_ddl(script: str) -> tuple[list[str], str]:
    """Pull ``CREATE ROLE`` / ``DROP ROLE`` (including a ``DO`` block that contains one) out of a script.

    The migrator runs everything else inside one transaction. Postgres rejects ``CREATE ROLE`` there.
    """
    roles: list[str] = []

    def take_do(match: re.Match[str]) -> str:
        block = match.group(0)
        if _ROLE_WORD.search(block):
            roles.append(block.strip())
            return ""
        return block

    body = _DO_BLOCK.sub(take_do, script)

    def take_role(match: re.Match[str]) -> str:
        roles.append(match.group(0).strip())
        return ""

    return roles, _ROLE_STMT.sub(take_role, body)
