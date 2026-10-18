"""Fake-model planner and SSE parsing. No network and no tenant ids."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import deploy_smoke  # noqa: E402


def test_plan_reads_the_skill_then_calls_the_directory_tool() -> None:
    cfg = deploy_smoke.load_cases(ROOT / "config" / "deploy_smoke.yaml")
    company = cfg.cases[0]
    messages = [{"role": "user", "content": company.question}, {"role": "system", "content": "user_id: P1"}]
    first = deploy_smoke.plan(messages, {"read_file", "search_memory"}, cfg)
    assert first.tool == "read_file"
    assert first.args is not None
    assert first.args["file_path"] == cfg.company_skill
    messages.append({"role": "assistant", "tool_calls": [{"function": {"name": "read_file"}}]})
    second = deploy_smoke.plan(messages, {"read_file", "org_lookup", "search_memory"}, cfg)
    assert second.tool == "org_lookup"
    assert second.args == {"who": "P1"}
    messages.append({"role": "assistant", "tool_calls": [{"function": {"name": "org_lookup"}}]})
    messages.append({"role": "tool", "content": '{"title": "Engineer"}'})
    done = deploy_smoke.plan(messages, {"org_lookup"}, cfg)
    assert done.kind == "text"
    assert "Engineer" in done.text
    assert not deploy_smoke._abstained(done.text, cfg)


def test_plan_abstains_when_the_tool_has_no_fact() -> None:
    cfg = deploy_smoke.load_cases(ROOT / "config" / "deploy_smoke.yaml")
    abstain = next(case for case in cfg.cases if case.expect.abstains)
    messages = [
        {"role": "user", "content": abstain.question},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "read_file"}}, {"function": {"name": "company_fact"}}],
        },
        {"role": "tool", "content": "No fact on record matches this question"},
    ]
    action = deploy_smoke.plan(messages, {"company_fact", "read_file"}, cfg)
    assert action.kind == "text"
    assert deploy_smoke._abstained(action.text, cfg)


def test_plan_memory_tool_is_always_bound() -> None:
    cfg = deploy_smoke.load_cases(ROOT / "config" / "deploy_smoke.yaml")
    memory = next(case for case in cfg.cases if case.id == "memory")
    action = deploy_smoke.plan([{"role": "user", "content": memory.question}], {"search_memory"}, cfg)
    assert action.tool == "search_memory"
    assert action.args == {"query": memory.question}


def test_parse_sse_events() -> None:
    raw = 'event: tool_start\ndata: {"tool": "search_memory"}\n\nevent: final\ndata: {"answer": "none", "status": "done"}\n\n'
    events = deploy_smoke.parse_sse(raw)
    assert events[0][0] == "tool_start"
    assert events[1][1]["status"] == "done"


def test_fake_server_models_and_completion() -> None:
    cfg = deploy_smoke.load_cases(ROOT / "config" / "deploy_smoke.yaml")
    httpd = deploy_smoke.make_server("127.0.0.1", 0, cfg)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as resp:
            assert resp.status == 200
        body = json.dumps(
            {
                "model": "local",
                "messages": [{"role": "user", "content": cfg.cases[1].question}],
                "tools": [{"type": "function", "function": {"name": "search_memory"}}],
            }
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        assert payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "search_memory"
    finally:
        httpd.shutdown()
