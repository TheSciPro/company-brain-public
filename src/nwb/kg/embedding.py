"""Adapters from our ``Embedder`` port (fastembed by default: local ONNX, free) to each library's interface."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient

try:
    from graphrag_sdk import Embedder as SdkEmbedderBase
except ImportError:
    SdkEmbedderBase = object  # Graphiti tests import this module without the GraphRAG extra

from nwb.memory.embeddings import Embedder, cosine


class SdkEmbedder(SdkEmbedderBase):
    """GraphRAG-SDK ``Embedder``."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner

    @property
    def model_name(self) -> str:
        return self._inner.model

    @property
    def dimension(self) -> int:
        return self._inner.dim

    def embed_query(self, text: str, **kwargs: Any) -> list[float]:
        return self._inner.embed([text])[0]

    def embed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        return self._inner.embed(texts) if texts else []


class GraphitiEmbedder(EmbedderClient):
    """Graphiti ``EmbedderClient``. Embedding is CPU-bound, so it runs in a worker thread."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner

    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        text = input_data if isinstance(input_data, str) else str(next(iter(input_data)))
        vecs = await asyncio.to_thread(self._inner.embed, [text])
        return vecs[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if not input_data_list:
            return []
        return await asyncio.to_thread(self._inner.embed, list(input_data_list))


class EmbeddingReranker(CrossEncoderClient):
    """Graphiti ``CrossEncoderClient`` that ranks by embedding cosine. Free and torch-less; good enough for
    the short facts Graphiti reranks. Swap in ``BGERerankerClient`` if sentence-transformers is installed."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        vecs = await asyncio.to_thread(self._inner.embed, [query, *passages])
        q, rest = vecs[0], vecs[1:]
        scored = [(p, max(0.0, cosine(q, v))) for p, v in zip(passages, rest, strict=True)]
        return sorted(scored, key=lambda x: (-x[1], x[0]))
