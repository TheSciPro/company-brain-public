"""GraphStore adapter over FalkorDB (server) or FalkorDBLite (embedded, for dev/tests)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from nwb.ports import Params

if TYPE_CHECKING:
    from nwb.config import Settings


class FalkorGraphStore:
    """Thin wrapper: ``ro_query`` for every read (AGENTS §5.5.3), ``query`` only in writers (jobs)."""

    def __init__(self, graph: Any, timeout_ms: int = 2000) -> None:
        self._graph = graph
        self._timeout = timeout_ms

    def query(self, cypher: str, params: Params | None = None) -> list[list[Any]]:
        return list(self._graph.query(cypher, dict(params or {}), timeout=self._timeout).result_set)

    def ro_query(self, cypher: str, params: Params | None = None) -> list[list[Any]]:
        return list(self._graph.ro_query(cypher, dict(params or {}), timeout=self._timeout).result_set)


def server_kwargs(s: Settings) -> dict[str, Any]:
    """Connection hygiene for a FalkorDB server (keepalive, idle health check, one retry), from settings."""
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError
    from redis.retry import Retry

    return {
        "socket_keepalive": s.falkor_keepalive,
        "health_check_interval": s.falkor_health_check_s,
        "socket_connect_timeout": s.falkor_connect_timeout_s,
        "retry": Retry(ExponentialBackoff(cap=1.0), s.falkor_retries),
        "retry_on_error": [RedisConnectionError, RedisTimeoutError, TimeoutError, OSError],
    }


def connect_server(
    host: str,
    port: int,
    graph_name: str,
    timeout_ms: int = 2000,
    *,
    username: str | None = None,
    password: str | None = None,
) -> FalkorGraphStore:
    """FalkorDB server or FalkorDB Cloud (free tier: plain TCP, username+password, no TLS)."""
    from falkordb import FalkorDB

    db = FalkorDB(host=host, port=port, username=username, password=password)
    return FalkorGraphStore(db.select_graph(graph_name), timeout_ms)


def connect_lite(path: Path, graph_name: str, timeout_ms: int = 2000) -> tuple[FalkorGraphStore, Any]:
    """Embedded FalkorDBLite. Returns (store, db) so callers keep the server process alive."""
    from redislite.falkordb_client import FalkorDB

    path.parent.mkdir(parents=True, exist_ok=True)
    db = FalkorDB(str(path))
    return FalkorGraphStore(db.select_graph(graph_name), timeout_ms), db
