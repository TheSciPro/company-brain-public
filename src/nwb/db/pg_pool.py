"""One way to open a Postgres connection pool, for the SQL layer and for LangGraph's saver and store.

Neon's pooler and scale-to-zero close idle server connections. A pool that hands such a connection out
again fails the next statement with ``OperationalError: SSL connection has been closed unexpectedly``.
Every pool here therefore:

- checks a connection before handing it out (``ConnectionPool.check_connection``) and replaces a dead one;
- closes connections idle longer than ``max_idle_s`` (above ``min_size``) and any older than ``max_lifetime_s``;
- asks the server for TCP keepalives, so a half-open socket is noticed instead of hanging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from nwb.config import Settings


@dataclass(frozen=True)
class PoolTuning:
    check: bool = True
    max_idle_s: float = 240.0
    max_lifetime_s: float = 1800.0
    keepalives_idle_s: int = 30
    keepalives_interval_s: int = 10
    keepalives_count: int = 3
    connect_timeout_s: int = 10

    @classmethod
    def from_settings(cls, s: Settings) -> PoolTuning:
        return cls(
            check=s.pg_pool_check,
            max_idle_s=s.pg_pool_max_idle_s,
            max_lifetime_s=s.pg_pool_max_lifetime_s,
            keepalives_idle_s=s.pg_keepalives_idle_s,
            keepalives_interval_s=s.pg_keepalives_interval_s,
            keepalives_count=s.pg_keepalives_count,
            connect_timeout_s=s.pg_connect_timeout_s,
        )

    def connect_kwargs(self) -> dict[str, Any]:
        """libpq keepalive and connect-timeout parameters, passed to every new connection."""
        return {
            "keepalives": 1,
            "keepalives_idle": self.keepalives_idle_s,
            "keepalives_interval": self.keepalives_interval_s,
            "keepalives_count": self.keepalives_count,
            "connect_timeout": self.connect_timeout_s,
        }


def open_pool(
    dsn: str,
    *,
    name: str,
    min_size: int,
    max_size: int,
    tuning: PoolTuning,
    kwargs: dict[str, Any],
) -> ConnectionPool[psycopg.Connection[Any]]:
    """An open pool. ``kwargs`` are the caller's connection options (row factory, autocommit, ...)."""
    return ConnectionPool(
        dsn,
        name=name,
        min_size=min_size,
        max_size=max_size,
        kwargs={**tuning.connect_kwargs(), **kwargs},
        check=ConnectionPool.check_connection if tuning.check else None,
        max_idle=tuning.max_idle_s,
        max_lifetime=tuning.max_lifetime_s,
        open=True,
    )
