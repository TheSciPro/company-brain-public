"""LangGraph persistence for the agent: a checkpointer (threads, ``thread_id = session_id``) and a store (the
``/memories/`` files, namespaced per (company, user)). Both follow ``Settings.agent_persistence``:

- ``postgres``: ``PostgresSaver`` + ``PostgresStore`` on ``DATABASE_URL`` (Neon in prod, local PG in dev);
- ``sqlite``: ``SqliteSaver`` + ``SqliteStore`` files under ``var/``;
- ``memory``: in-process (tests).
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore
from langgraph.store.sqlite import SqliteStore
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row

from nwb.config import Settings
from nwb.db.app_role import agent_tables_ready, grant_agent_storage
from nwb.db.pg_pool import PoolTuning, open_pool
from nwb.errors import ConfigError


@dataclass
class AgentPersistence:
    checkpointer: BaseCheckpointSaver[Any]
    store: BaseStore
    _conns: list[Any] = field(default_factory=list, repr=False)

    def close(self) -> None:
        for c in self._conns:
            with contextlib.suppress(Exception):  # closing at shutdown; nothing useful to do on failure
                c.close()
        self._conns.clear()


def prepare_agent_storage(dsn: str) -> None:
    """Create LangGraph tables on the owner connection and grant DML to ``nw_app``.

    ``setup()`` runs ``CREATE INDEX CONCURRENTLY``, which needs autocommit and a role that can CREATE.
    The app role cannot, so ``nwb migrate`` calls this and the runtime skips ``setup()`` once the tables exist.
    """
    conn = Connection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    try:
        PostgresSaver(conn).setup()
        PostgresStore(conn).setup()
        grant_agent_storage(conn)
    finally:
        conn.close()


def build_persistence(s: Settings) -> AgentPersistence:
    kind = s.agent_persistence
    if kind == "memory":
        return AgentPersistence(InMemorySaver(), InMemoryStore())
    if kind == "postgres":
        # One checked pool for both: a connection Neon closed while idle is replaced, not reused.
        pool = open_pool(
            s.pg_dsn,
            name="agent",
            min_size=s.pg_pool_min,
            max_size=s.pg_pool_max,
            tuning=PoolTuning.from_settings(s),
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        saver = PostgresSaver(pool)
        store = PostgresStore(pool)
        with pool.connection() as conn:
            ready = agent_tables_ready(conn)
        if not ready:
            try:
                saver.setup()
                store.setup()
            except InsufficientPrivilege as exc:
                pool.close()
                raise ConfigError(
                    "LangGraph checkpoint tables are missing and this login cannot create them. "
                    "Run `nwb migrate` as the database owner (NWB_MIGRATE_URL)."
                ) from exc
        return AgentPersistence(saver, store, [pool])

    for p in (s.checkpoint_path, s.store_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    cconn = sqlite3.connect(str(s.checkpoint_path), check_same_thread=False)
    sconn = sqlite3.connect(str(s.store_path), check_same_thread=False, isolation_level=None)
    sstore = SqliteStore(sconn)
    sstore.setup()
    return AgentPersistence(SqliteSaver(cconn), sstore, [cconn, sconn])
