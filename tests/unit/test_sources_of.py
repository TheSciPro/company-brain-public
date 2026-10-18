from langchain_core.messages import ToolMessage

from nwb.agent.runner import sources_of, tool_end_events


def test_fact_citation_carries_its_level() -> None:
    art = {
        "question": "discount",
        "candidates": [
            {
                "key": "discount_pct",
                "answer": {"value": "18", "citation": {"id": "PEX-1", "kind": "fact", "level": "highly_confidential"}},
            },
            {
                "key": "other",
                "answer": {"value": "1", "citation": {"id": "PEX-1", "kind": "fact", "level": "highly_confidential"}},
            },
        ],
    }
    assert sources_of(art) == {
        "sources": [{"id": "PEX-1", "kind": "fact", "title": None, "level": "highly_confidential"}]
    }


def test_search_hits_use_classification_and_name_unopened_levels() -> None:
    art = {
        "query": "brightmart",
        "hits": [
            {
                "id": "doc-1",
                "kind": "document",
                "title": "SOW",
                "snippet": "…",
                "classification": "internal",
                "score": 0.4,
            }
        ],
        "searched": ["public", "internal"],
        "not_searched": [{"source": "highly_confidential", "reason": "access"}],
    }
    assert sources_of(art) == {
        "sources": [{"id": "doc-1", "kind": "document", "title": "SOW", "level": "internal"}],
        "searched": ["public", "internal"],
        "not_searched": [{"source": "highly_confidential", "reason": "access"}],
    }


def test_no_artifact_adds_nothing_to_tool_end() -> None:
    assert sources_of(None) == {}
    (event,) = tool_end_events("run-1", ToolMessage(content="ok", tool_call_id="t1", name="read_file"))
    assert set(event.data) == {"tool", "status", "preview", "subagent"}


def test_graph_evidence_reaches_the_ui_but_not_the_model() -> None:
    import json

    from nwb.agent.tools.base import result
    from nwb.knowledge.models import Citation, FactLookup, FactValue, KeyAnswer

    cite = Citation(
        id="approvals/pricing_exceptions:PEX-1",
        kind="fact",
        level="highly_confidential",
        graph="nw_kb_highly_confidential",
        node_id="F-1",
        node_label="Fact",
        query="MATCH (f:Fact) WHERE f.key = $key",
    )
    lookup = FactLookup(
        question="discount?",
        decision_id="dec-0123456789",
        candidates=[
            KeyAnswer(
                key="discount_pct", asof="2026-09-28", answer=FactValue(value="18", status="current", citation=cite)
            )
        ],
    )
    content, artifact = result(lookup)
    model_view = json.loads(content)
    assert "decision_id" not in model_view
    assert set(model_view["candidates"][0]["answer"]["citation"]) == {"id", "kind", "level"}
    assert artifact["decision_id"] == "dec-0123456789"
    (source,) = sources_of(artifact)["sources"]
    assert source["graph"] == "nw_kb_highly_confidential" and source["node_label"] == "Fact"
    assert source["query"].startswith("MATCH (f:Fact)")
