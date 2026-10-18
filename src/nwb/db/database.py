"""SqlDatabase adapters. Transactions are owned by the unit of work, never by repositories."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from nwb.db.app_role import APP_ROLE, assert_app_role
from nwb.db.dialect import to_dialect_params
from nwb.db.pg_pool import PoolTuning, open_pool
from nwb.obs.log import get_logger
from nwb.ports import Dialect, Params, Row

log = get_logger(__name__)

sqlite3.register_adapter(date, date.isoformat)  # explicit: the default adapter is deprecated


class SqliteUnitOfWork:
    dialect = Dialect.SQLITE

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Params | None = None) -> None:
        self._conn.execute(sql, dict(params or {}))

    def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        self._conn.executemany(sql, [dict(r) for r in rows])

    def fetchall(self, sql: str, params: Params | None = None) -> list[Row]:
        cur = self._conn.execute(sql, dict(params or {}))
        cols = [c[0] for c in cur.description or ()]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def fetchone(self, sql: str, params: Params | None = None) -> Row | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def executescript(self, script: str) -> None:
        # executescript would COMMIT implicitly; run statement by statement inside our transaction
        for stmt in _split_sql(script):
            self._conn.execute(stmt)


def _register_sqlite_adapters() -> None:
    """Explicit ISO-8601 adapters (the implicit datetime adapter is deprecated since Python 3.12)."""
    sqlite3.register_adapter(datetime, lambda v: v.isoformat(sep=" "))
    sqlite3.register_adapter(date, lambda v: v.isoformat())


_register_sqlite_adapters()


class SqliteDatabase:
    """SQLite fallback. Access is enforced by the app-level CTE (weaker than RLS: same tests must pass)."""

    dialect = Dialect.SQLITE

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        # One shared connection: transactions are serialised across threads (parallel search lanes, API workers).
        self._lock = threading.Lock()
        self._owner: int | None = None

    @contextmanager
    def unit_of_work(
        self, *, as_person: str | None = None, asof: date | None = None, as_company: str | None = None
    ) -> Iterator[SqliteUnitOfWork]:
        del as_person, asof, as_company  # SQLite has no session variables; access is applied by queries
        if self._owner == threading.get_ident():
            raise RuntimeError("nested unit_of_work on the same thread; pass the open uow down instead")
        with self._lock:
            self._owner = threading.get_ident()
            self._conn.execute("BEGIN")
            try:
                yield SqliteUnitOfWork(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._owner = None

    def close(self) -> None:
        self._conn.close()


class PostgresUnitOfWork:
    dialect = Dialect.POSTGRES

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Params | None = None) -> None:
        self._conn.execute(to_dialect_params(sql, self.dialect), dict(params or {}))

    def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(to_dialect_params(sql, self.dialect), [dict(r) for r in rows])

    def fetchall(self, sql: str, params: Params | None = None) -> list[Row]:
        return self._conn.execute(to_dialect_params(sql, self.dialect), dict(params or {})).fetchall()

    def fetchone(self, sql: str, params: Params | None = None) -> Row | None:
        return self._conn.execute(to_dialect_params(sql, self.dialect), dict(params or {})).fetchone()

    def executescript(self, script: str) -> None:
        self._conn.execute(script.encode())  # simple-query protocol: multi-statement, no params


class PostgresDatabase:
    """Postgres 17 with RLS.

    Production connects *as* ``nw_app`` (``require_app_role=True``). A superuser test connection
    switches with ``SET LOCAL ROLE`` only when the statement names a person. ``app.company_id``
    is that person's company unless ``as_company`` is passed.
    """

    dialect = Dialect.POSTGRES

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        statement_timeout_ms: int = 5000,
        require_app_role: bool = False,
        tuning: PoolTuning | None = None,
    ) -> None:
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        # The timeout is set per transaction (``unit_of_work``), not as a startup ``options`` parameter:
        # Neon's pooler (PgBouncer, transaction mode) rejects startup options.
        self._pool: ConnectionPool[psycopg.Connection[Any]] = open_pool(
            dsn,
            name="sql",
            min_size=min_size,
            max_size=max_size,
            tuning=tuning or PoolTuning(),
            kwargs={"row_factory": dict_row},
        )
        if require_app_role:
            try:
                with self._pool.connection() as conn:
                    assert_app_role(conn)
            except BaseException:
                self._pool.close()
                raise

    @contextmanager
    def unit_of_work(
        self, *, as_person: str | None = None, asof: date | None = None, as_company: str | None = None
    ) -> Iterator[PostgresUnitOfWork]:
        conn = self._begin()
        try:
            uow = PostgresUnitOfWork(conn)
            # No person: row-level security returns no classified rows.
            if as_person is not None:
                _assume_app_role(conn)
                company = as_company if as_company is not None else _company_of(conn, as_person)
                conn.execute(
                    "SELECT set_config('app.person_id', %s, true),"
                    " set_config('app.asof', %s, true),"
                    " set_config('app.company_id', %s, true)",
                    (as_person, (asof or date.today()).isoformat(), company),
                )
            yield uow
        except BaseException:
            with suppress(psycopg.Error):  # a dead connection cannot roll back; the original error is raised
                conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            self._pool.putconn(conn)

    def _begin(self) -> psycopg.Connection[Any]:
        """A connection inside a new transaction, its first statement already run.

        The first statement is retried once on another connection when the server had closed this one
        (``OperationalError``). Nothing of the unit has run yet, so the retry is safe for reads and writes.
        The pool discards the broken connection when it is returned.
        """
        conn = self._pool.getconn()
        try:
            self._start(conn)
            return conn
        except psycopg.OperationalError as exc:
            self._pool.putconn(conn)
            log.warning("postgres connection was closed by the server; retrying once on a new one: %s", exc)
        except BaseException:
            self._pool.putconn(conn)
            raise
        conn = self._pool.getconn()
        try:
            self._start(conn)
        except BaseException:
            self._pool.putconn(conn)
            raise
        return conn

    def _start(self, conn: psycopg.Connection[Any]) -> None:
        conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{self._statement_timeout_ms}ms",))

    def execute_outside_transaction(self, statement: str) -> None:
        """Autocommit one statement. ``CREATE ROLE`` cannot run inside the migrator's transaction."""
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute(statement)

    def close(self) -> None:
        self._pool.close()


def _assume_app_role(conn: psycopg.Connection[dict[str, Any]]) -> None:
    row = conn.execute("SELECT current_user AS u").fetchone()
    if row is None or row["u"] != APP_ROLE:
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(APP_ROLE)))


def _company_of(conn: psycopg.Connection[dict[str, Any]], person_id: str) -> str:
    row = conn.execute("SELECT company_id FROM people WHERE id = %s", (person_id,)).fetchone()
    if row is None or row["company_id"] is None:
        return ""
    return str(row["company_id"])


def _split_sql(script: str) -> list[str]:
    """Split a SQL script on ';' at line ends, ignoring ``--`` comments. Enough for our own migrations."""
    out: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        code = line.split("--", 1)[0]
        buf.append(code)
        if code.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out
