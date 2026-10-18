"""A pooled connection the server closed while idle (Neon's pooler, scale-to-zero) is not handed out again.

Each test kills the pool's backend with ``pg_terminate_backend``, as Neon does to an idle connection, and
then expects the next request to succeed.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from nwb.agent.persistence import build_persistence
from nwb.config import Settings
from nwb.db.database import PostgresDatabase
from nwb.db.pg_pool import PoolTuning
from tests.conftest import PG_DSN, PG_OK, reset_pg

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not PG_OK, reason="no local Postgres")]


def _kill_backends(pids: set[int]) -> None:
    with psycopg.connect(PG_DSN, autocommit=True) as admin:
        for pid in pids:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))


def _pid(db: PostgresDatabase) -> int:
    with db.unit_of_work() as uow:
        row = uow.fetchone("SELECT pg_backend_pid() AS pid")
    assert row is not None
    return int(row["pid"])


@pytest.mark.parametrize("check", [True, False], ids=["pool-check", "retry-first-statement"])
def test_sql_unit_of_work_survives_a_closed_connection(check: bool) -> None:
    db = PostgresDatabase(PG_DSN, max_size=1, tuning=PoolTuning(check=check))
    try:
        dead = _pid(db)
        _kill_backends({dead})
        assert _pid(db) != dead  # a fresh connection, not "SSL connection has been closed unexpectedly"
    finally:
        db.close()


def test_sql_pool_sets_keepalives_and_idle_limits() -> None:
    db = PostgresDatabase(PG_DSN, max_size=1, tuning=PoolTuning(max_idle_s=99.0, keepalives_idle_s=17))
    try:
        assert db._pool.kwargs["keepalives"] == 1 and db._pool.kwargs["keepalives_idle"] == 17
        assert db._pool.max_idle == 99.0
        assert db._pool._check is not None
    finally:
        db.close()


def test_langgraph_store_and_saver_survive_a_closed_connection() -> None:
    reset_pg()
    settings = Settings(database_url=PG_DSN, pg_pool_max=1)
    persistence = build_persistence(settings)
    try:
        store = persistence.store
        store.put(("NW", "NW0301"), "USER.md", {"content": "short answers"})
        pool: Any = persistence._conns[0]
        with pool.connection() as conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        _kill_backends({pid})
        item = store.get(("NW", "NW0301"), "USER.md")  # langgraph's batch get on a checked connection
        assert item is not None and item.value == {"content": "short answers"}
        assert persistence.checkpointer.get_tuple({"configurable": {"thread_id": "none"}}) is None
    finally:
        persistence.close()
