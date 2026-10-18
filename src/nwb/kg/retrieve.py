"""Read a company graph with GraphRAG-SDK ``GraphRAG.retrieve``.

``retrieve`` returns context only. ``completion`` is not used: the agent writes
the answer, and a second model draft would be another place that can guess.
No Cypher is written here. Text-to-Cypher, when ``enable_cypher`` is on, is the
SDK's own retrieval path.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

from graphrag_sdk import ConnectionConfig, GraphRAG
from graphrag_sdk.core.providers.base import LLMInterface

from nwb.conf.models import GraphConfig
from nwb.config import Settings
from nwb.kg.embedding import SdkEmbedder
from nwb.kg.models import GraphModels
from nwb.kg.recording import CallRecorder, RecordMode
from nwb.kg.server import FalkorEndpoint
from nwb.knowledge.retrieve import GraphPassage, GraphUnavailableError
from nwb.llm.factory import LLMFactory
from nwb.llm.settings import LLMSettings
from nwb.memory.embeddings import Embedder, cosine
from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)


class SdkCompanyRetriever:
    """One ``GraphRAG.retrieve`` call per graph the caller is allowed to open."""

    def __init__(
        self,
        endpoint: FalkorEndpoint,
        llm: LLMInterface,
        embedder: Embedder,
        *,
        dimension: int,
        enable_cypher: bool,
        query_timeout_ms: int,
    ) -> None:
        self._endpoint = endpoint
        self._llm = llm
        self._embedder = embedder
        self._dimension = dimension
        self._cypher = enable_cypher
        self._timeout_ms = query_timeout_ms

    def retrieve(self, graph: str, question: str, limit: int) -> list[GraphPassage]:
        try:
            result = _run(self._retrieve(graph, question))
        except GraphUnavailableError:
            raise
        except Exception as exc:
            log.warning("graph retrieve failed", graph=graph, error=type(exc).__name__)
            raise GraphUnavailableError(graph) from None
        passages: list[GraphPassage] = []
        for item in result.items:
            text = (item.content or "").strip()
            if not text:
                continue
            section = item.metadata.get("section") if item.metadata else None
            score = item.score if item.score is not None else _similarity(self._embedder, question, text)
            passages.append(GraphPassage(content=text, section=str(section) if section else None, score=score))
            if len(passages) >= limit:
                break
        dataflow(Hop.RETRIEVAL, kind="company_search", phase="sdk", graph=graph, passages=len(passages))
        return passages

    async def _retrieve(self, graph: str, question: str) -> Any:
        conn = ConnectionConfig(
            host=self._endpoint.host,
            port=self._endpoint.port,
            username=self._endpoint.username,
            password=self._endpoint.password,
            ssl=self._endpoint.ssl,
            graph_name=graph,
            query_timeout_ms=self._timeout_ms,
        )
        async with GraphRAG(
            connection=conn,
            llm=self._llm,
            embedder=SdkEmbedder(self._embedder),
            embedding_dimension=self._dimension,
            enable_cypher=self._cypher,
        ) as rag:
            return await rag.retrieve(question)


def _similarity(embedder: Embedder, question: str, text: str) -> float:
    """Cosine of the query and one passage. The SDK retrieve items omit this score."""
    vectors = embedder.embed([question, text])
    if len(vectors) < 2:
        return 0.0
    return cosine(vectors[0], vectors[1])


def build_sdk_retriever(
    settings: Settings,
    cfg: GraphConfig,
    embedder: Embedder,
    endpoint: FalkorEndpoint,
) -> SdkCompanyRetriever:
    """Live model, no cassette writes. ``endpoint`` is the shared company-graph target."""
    recorder = CallRecorder(settings.log_dir / "llm_calls", RecordMode.LIVE)
    llm = GraphModels(LLMFactory(LLMSettings()), cfg).sdk_llm(recorder)
    return SdkCompanyRetriever(
        endpoint,
        llm,
        embedder,
        dimension=embedder.dim,
        enable_cypher=cfg.company.enable_cypher,
        query_timeout_ms=cfg.company.query_timeout_ms,
    )


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` from sync tool code. A worker thread is used when a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: list[T] = []
    err: list[BaseException] = []

    def _worker() -> None:
        try:
            box.append(asyncio.run(coro))
        except BaseException as exc:
            err.append(exc)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return box[0]
