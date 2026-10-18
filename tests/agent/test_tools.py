"""Domain tools: identity only from the runtime, typed artifacts, errors as error ToolMessages."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage

from nwb.knowledge.models import Citation, FactValue, KeyAnswer
from tests.agent.conftest import Harness

IDENTITY = {"user_id", "person_id", "owner_id", "asof", "company_id", "session_id", "runtime", "requester"}


def _runtime(ctx: Any) -> ToolRuntime[Any, Any]:
    return ToolRuntime(state={}, context=ctx, config={}, stream_writer=lambda _: None, tool_call_id="t", store=None)


def _invoke(harness: Harness, name: str, args: dict[str, Any], ctx: Any) -> ToolMessage:
    tool = harness.c.tools[name]
    out = tool.invoke({"type": "tool_call", "id": "t", "name": name, "args": {**args, "runtime": _runtime(ctx)}})
    assert isinstance(out, ToolMessage)
    return out


def test_no_tool_schema_carries_identity(harness: Harness) -> None:
    for name, tool in harness.c.tools.items():
        props = set(tool.tool_call_schema.model_json_schema().get("properties", {}))
        assert not props & IDENTITY, name


def test_every_tool_is_documented_and_declares_its_side_effect(harness: Harness) -> None:
    for name, tool in harness.c.tools.items():
        assert len(tool.description) > 40, name
        assert (tool.metadata or {}).get("side_effect") in {"none", "write_memory", "write_action", "external"}


def test_tool_without_runtime_context_refuses(harness: Harness) -> None:
    out = _invoke(harness, "company_fact", {"question": "How many days a week can I work from home?"}, None)
    assert out.status == "error" and "runtime context" in str(out.content)


def test_domain_errors_become_error_tool_messages(harness: Harness) -> None:
    out = _invoke(harness, "org_lookup", {"who": "zzzz-no-such-person"}, harness.ctx())
    assert out.status == "error" and "no person matching" in str(out.content)


def test_results_carry_a_structured_artifact(harness: Harness) -> None:
    out = _invoke(harness, "company_fact", {"question": "How many days a week can I work from home?"}, harness.ctx())
    assert out.status == "success" and isinstance(out.artifact, dict) and out.artifact["candidates"]


def test_search_company_output_has_no_hidden_count(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    """A match the asker cannot see is omitted. The tool result and logs do not say how many."""
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    out = _invoke(harness, "search_company", {"query": "Brightmart discount"}, harness.ctx("NW1103"))
    assert out.status == "success"
    content = str(out.content)
    artifact = out.artifact
    assert isinstance(artifact, dict)
    assert "hidden_matches" not in artifact
    assert "hidden match" not in content.lower()
    assert "DEC-2026-005" not in {hit["id"] for hit in artifact["hits"]}
    assert "hidden_matches" not in json.dumps(artifact)
    for rec in caplog.records:
        if not rec.name.startswith("nwb"):
            continue
        fields = getattr(rec, "fields", None) or {}
        assert "hidden" not in fields
        assert "hidden match" not in rec.getMessage().lower()


def test_a_fact_reply_is_a_natural_sentence_from_its_template(harness: Harness) -> None:
    """sources.yaml ``say`` + ``unit_words``: the client's name, no "pct", the unit on the value, then the citation."""
    kb = harness.c.knowledge
    client = kb.clients.resolve("Brightmart")
    assert client is not None
    value = FactValue(
        value="18",
        unit="%",
        status="current",
        scope=f"client={client.id}",
        citation=Citation(id="approvals/pricing_exceptions:PEX-1", kind="fact"),
    )
    best = KeyAnswer(key="discount_pct", label="discount pct", asof=harness.actor().asof, answer=value)
    sentence = kb._sentence(best)
    assert sentence == f"The final approved discount for {client.name} is 18%.", sentence


def test_an_empty_search_gives_the_not_on_record_reply(harness: Harness) -> None:
    """direct_reply_tools: nothing found ends the turn with the same abstention as company_fact."""
    out = _invoke(harness, "search_company", {"query": "zzqx unknown flibbertigibbet"}, harness.ctx("NW1103"))
    assert out.artifact["hits"] == [] and out.artifact["stop"] is True
    assert out.artifact["say"].startswith("This is not on record for you.")
