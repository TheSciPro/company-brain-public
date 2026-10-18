"""Retrieval port for company graphs. The SDK adapter lives in ``nwb.kg.retrieve``.

This module does not import GraphRAG-SDK. A base install still constructs the
search tool and fails closed (no passages, no document scan) when the extra is
absent. An empty ``FALKORDB_URL`` is not that case: load and search share
``resolve_company_falkor`` (the URL, ``falkor_target()``, or ``graph.yaml``
``server.local_dir``).
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nwb.conf.models import GraphConfig
from nwb.config import Settings
from nwb.errors import ConfigError, NwbError
from nwb.kg.server import CompanyFalkorTarget, FalkorEndpoint, LocalFalkorServer, resolve_company_falkor
from nwb.memory.embeddings import Embedder
from nwb.obs.log import get_logger

log = get_logger(__name__)


class GraphUnavailableError(NwbError):
    """The named graph could not be read. The message does not describe its contents."""

    def __init__(self, graph: str) -> None:
        self.graph = graph
        super().__init__("company graph is unavailable")


@dataclass(frozen=True, slots=True)
class GraphPassage:
    """One piece of context ``GraphRAG.retrieve`` actually returned."""

    content: str
    section: str | None = None
    score: float | None = None


class CompanyGraphRetriever(Protocol):
    def retrieve(self, graph: str, question: str, limit: int) -> list[GraphPassage]:
        """Passages from one graph. Raise ``GraphUnavailableError`` when it cannot be read."""
        ...


class UnavailableRetriever:
    """Fail closed. Does not scan documents and does not invent passages."""

    def retrieve(self, graph: str, question: str, limit: int) -> list[GraphPassage]:
        del question, limit
        raise GraphUnavailableError(graph)


class _ConfiguredRetriever:
    """Reads the FalkorDB ``resolve_company_falkor`` chose.

    A local server starts on the first retrieve and is stopped by ``close`` (and
    at process exit). Its data directory is the one the loader writes.
    """

    def __init__(
        self,
        settings: Settings,
        cfg: GraphConfig,
        embedder: Embedder,
        target: CompanyFalkorTarget,
        build_sdk: Callable[[Settings, GraphConfig, Embedder, FalkorEndpoint], CompanyGraphRetriever],
        endpoint: FalkorEndpoint | None = None,
    ) -> None:
        self._settings = settings
        self._cfg = cfg
        self._embedder = embedder
        self._target = target
        self._build_sdk = build_sdk
        self._fixed_endpoint = endpoint
        self._inner: CompanyGraphRetriever | None = None
        self._server: LocalFalkorServer | None = None

    def retrieve(self, graph: str, question: str, limit: int) -> list[GraphPassage]:
        try:
            return self._ready().retrieve(graph, question, limit)
        except GraphUnavailableError:
            raise
        except Exception as exc:
            log.warning("company graph retrieve failed", reason=type(exc).__name__)
            raise GraphUnavailableError(graph) from None

    def close(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
            self._inner = None

    def _ready(self) -> CompanyGraphRetriever:
        if self._inner is None:
            self._inner = self._build_sdk(self._settings, self._cfg, self._embedder, self._endpoint())
        return self._inner

    def _endpoint(self) -> FalkorEndpoint:
        if self._fixed_endpoint is not None:
            return self._fixed_endpoint
        if self._target.endpoint is not None:
            return self._target.endpoint
        if self._server is None:
            if self._target.local_dir is None:
                raise ConfigError("company FalkorDB target has neither an endpoint nor a local directory")
            server = LocalFalkorServer(
                self._target.local_dir,
                port=self._target.local_port,
                persist=self._target.persist,
            )
            server.start()
            self._server = server
            atexit.register(server.stop)
        return FalkorEndpoint(host=self._server.host, port=self._server.port)


def build_company_retriever(
    settings: Settings,
    cfg: GraphConfig,
    embedder: Embedder,
    endpoint: FalkorEndpoint | None = None,
) -> CompanyGraphRetriever:
    """SDK retriever for the same FalkorDB the company-graph loader writes.

    The import is inside this function because ``nwb.kg.retrieve`` depends on the
    optional ``graph`` extra (GraphRAG-SDK). Importing it at module level would
    stop the base install, and the tests, from loading the agent. A missing extra,
    or a target that cannot be resolved, fails closed. An empty ``FALKORDB_URL``
    does not: it uses ``falkor_target()`` or ``graph.yaml`` ``server.local_dir``.
    ``endpoint``, when passed, is a server the caller already opened for that same
    target, so this retriever does not start a second one.
    """
    try:
        from nwb.kg.retrieve import build_sdk_retriever
    except ImportError:
        log.warning("company graph retriever off", reason="graph_extra_missing")
        return UnavailableRetriever()
    try:
        target = resolve_company_falkor(settings, cfg.server)
    except Exception as exc:
        log.warning("company graph retriever off", reason=type(exc).__name__)
        return UnavailableRetriever()
    return _ConfiguredRetriever(settings, cfg, embedder, target, build_sdk_retriever, endpoint)
