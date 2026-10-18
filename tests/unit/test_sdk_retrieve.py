"""The company retriever calls GraphRAG-SDK ``retrieve``, not a hand-written query."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")

from graphrag_sdk.core.models import RetrieverResult, RetrieverResultItem

from nwb.kg.retrieve import SdkCompanyRetriever
from nwb.kg.server import FalkorEndpoint
from nwb.memory.embeddings import HashingEmbedder


class _QuietLLM:
    model_name = "offline"
    max_concurrency = 1

    def invoke(self, prompt: str, **kwargs: Any) -> Any:
        del prompt, kwargs
        raise AssertionError("retrieve must not be replaced by a local completion")


def test_sdk_retriever_uses_graph_rag_retrieve(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: dict[str, Any] = {}

    class _FakeRAG:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args
            opened["graph"] = kwargs["connection"].graph_name
            opened["cypher"] = kwargs["enable_cypher"]

        async def __aenter__(self) -> _FakeRAG:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def retrieve(self, question: str, **kwargs: Any) -> RetrieverResult:
            del kwargs
            opened["question"] = question
            return RetrieverResult(
                items=[
                    RetrieverResultItem(content="  handbook passage  ", metadata={"section": "passages"}, score=0.5),
                    RetrieverResultItem(content="   ", metadata={"section": "facts"}, score=0.1),
                ]
            )

    monkeypatch.setattr("nwb.kg.retrieve.GraphRAG", _FakeRAG)
    retriever = SdkCompanyRetriever(
        FalkorEndpoint(host="127.0.0.1", port=6379),
        _QuietLLM(),  # type: ignore[arg-type]
        HashingEmbedder(dim=8),
        dimension=8,
        enable_cypher=True,
        query_timeout_ms=1000,
    )
    passages = retriever.retrieve("acme_kb_public", "who holds the handbook", limit=6)
    assert opened["graph"] == "acme_kb_public"
    assert opened["cypher"] is True
    assert opened["question"] == "who holds the handbook"
    assert len(passages) == 1
    assert passages[0].content == "handbook passage"
    assert passages[0].section == "passages"
    assert passages[0].score == 0.5
