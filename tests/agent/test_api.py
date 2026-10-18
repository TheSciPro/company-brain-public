"""HTTP surface: identity from header only, SSE event stream, error mapping, one trace id per chat."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from nwb.api.app import create_app
from nwb.obs.log import setup_logging
from tests.agent.conftest import Harness
from tests.fakes import call

H = {"X-NWB-Person": "NW1103"}


def test_personas_and_health(harness: Harness) -> None:
    client = TestClient(create_app(harness.c))
    assert client.get("/health").json()["status"] == "ok"
    assert any(p["id"] == "NW2027" for p in client.get("/personas").json())


def test_unknown_or_missing_person_is_forbidden(harness: Harness) -> None:
    client = TestClient(create_app(harness.c))
    assert client.post("/sessions", json={}).status_code == 403
    assert client.post("/sessions", json={}, headers={"X-NWB-Person": "NOPE"}).status_code == 403


def test_identity_cannot_be_smuggled_in_the_body(harness: Harness) -> None:
    client = TestClient(create_app(harness.c))
    r = client.post("/chat", json={"message": "hi", "person_id": "NW0201"}, headers=H)
    assert r.status_code == 422


def test_chat_streams_sse_events(harness: Harness) -> None:
    harness.model.script = [
        call("grep", {"pattern": "status", "path": "/memories/facts", "output_mode": "content"}),
        AIMessage(content="Nothing saved yet."),
    ]
    client = TestClient(create_app(harness.c))
    sid = client.post("/sessions", json={}, headers=H).json()["session_id"]
    with client.stream("POST", "/chat", json={"message": "what do you know?", "session_id": sid}, headers=H) as r:
        body = "".join(r.iter_text())
    for ev in ("run_started", "tool_start", "tool_end", "final"):
        assert f"event: {ev}" in body
    assert "Nothing saved yet." in body


def _markers(records: list[dict[str, object]]) -> list[str]:
    out: list[str] = []
    for record in records:
        if record.get("msg") == "response out":
            out.append("response out")
        elif hop := record.get("hop"):
            if hop == "agent.run":
                out.append(f"agent.run:{record.get('event')}")
            else:
                out.append(str(hop))
    return out


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    at = 0
    for item in haystack:
        if at < len(needles) and item == needles[at]:
            at += 1
    return at == len(needles)


def test_one_trace_id_and_hop_order(harness: Harness, tmp_path: Path) -> None:
    """One chat is one trace id. response out is after the agent run, with status and duration."""
    settings = harness.c.settings.model_copy(
        update={
            "log_dir": tmp_path,
            "app_env": "dev",
            "log_level": "DEBUG",
            "log_format": "json",
            "log_file": True,
            "dataflow_trace": True,
        }
    )
    setup_logging(settings)
    try:
        harness.model.script = [call("search_memory", {"query": "status"}), AIMessage(content="Nothing saved yet.")]
        client = TestClient(create_app(harness.c))
        with client.stream("POST", "/chat", json={"message": "what do you know?"}, headers=H) as response:
            assert response.status_code == 200
            tid = response.headers["x-trace-id"]
            body = "".join(response.iter_text())
        assert "event: final" in body
        assert tid and tid != "-"

        app_log = tmp_path / "app.jsonl"
        records = [json.loads(line) for line in app_log.read_text().splitlines() if line.strip()]
        chat = [row for row in records if row.get("trace_id") == tid]
        assert chat, "no log lines carried the response trace id"
        assert {row["trace_id"] for row in chat} == {tid}
        agent_runs = [row for row in records if row.get("hop") == "agent.run"]
        assert agent_runs and {row["trace_id"] for row in agent_runs} == {tid}

        order = _markers(chat)
        expected = [
            "session",
            "api",
            "agent.run:start",
            "agent.payload",
            "tool.call",
            "agent.run:end",
            "response out",
        ]
        assert _is_subsequence(expected, order), order
        finished = [row for row in chat if row.get("msg") == "response out"]
        assert len(finished) == 1
        assert finished[0]["status"] == "done"
        assert isinstance(finished[0]["ms"], int) and finished[0]["ms"] >= 0
        assert order.index("response out") > order.index("agent.run:end")
    finally:
        root = logging.getLogger()
        for handler in [h for h in root.handlers if getattr(h, "_nwb_handler", False)]:
            root.removeHandler(handler)
            handler.close()


def test_other_users_session_is_not_found(harness: Harness) -> None:
    client = TestClient(create_app(harness.c))
    sid = client.post("/sessions", json={}, headers=H).json()["session_id"]
    r = client.post(f"/sessions/{sid}/end", headers={"X-NWB-Person": "NW0201"})
    assert r.status_code == 404
