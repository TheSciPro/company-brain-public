"""Catalog and update-gate tests that do not need a loaded company."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from langchain.tools import ToolRuntime

from nwb.agent.context import RuntimeContext
from nwb.conf import ConfigLoader, SourcesConfig
from nwb.conf.models import ConnectorDecl, ConnectorFields
from nwb.connectors.agents import ConnectorBindings, make_update_tool, path_allowed
from nwb.connectors.catalog import SourceSpec, list_sources
from nwb.connectors.read import _SELECTS
from tests.conftest import ROOT

_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|attach|pragma)\b", re.I)


def _sources() -> SourcesConfig:
    return ConfigLoader(ROOT / "config").load("sources", SourcesConfig)


def _ctx() -> RuntimeContext:
    return RuntimeContext("co", "NW1103", "s-test", "run-test", "2026-09-28", "m-1")


def _invoke(tool: Any, args: dict[str, Any], ctx: RuntimeContext) -> Any:
    runtime = ToolRuntime(state={}, context=ctx, config={}, stream_writer=lambda _: None, tool_call_id="t", store=None)
    return tool.invoke({"type": "tool_call", "id": "t", "name": tool.name, "args": {**args, "runtime": runtime}})


class RecordingSink:
    name = "test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def execute(self, kind: str, target_ref: str, title: str, message: str) -> str:
        self.calls.append((kind, target_ref, title, message))
        return "ext-9"


def test_connector_modules_log_only_through_obs() -> None:
    root = ROOT / "src" / "nwb" / "connectors"
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "logging" for alias in node.names), path.name
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "logging", path.name


def test_every_catalog_entry_is_one_spec() -> None:
    cfg = _sources()
    specs = list_sources(cfg)
    expected = [*cfg.documents.folders, *cfg.records, "directory", *(item.id for item in cfg.connectors)]
    assert [spec.id for spec in specs] == expected
    assert len(specs) == len({spec.agent_name for spec in specs})
    assert [spec.id for spec in specs if spec.update_tool] == ["slack"]
    gmail = next(spec for spec in specs if spec.id == "gmail")
    assert gmail.query_tool == "query_gmail" and gmail.update_tool is None and gmail.writes == ()


def test_adding_a_connector_is_a_config_change_only() -> None:
    notes = ConnectorDecl(
        id="notes",
        description="A connected notes app used only in this test.",
        list_action="NOTES_LIST",
        fields=ConnectorFields(external_id="id", title="title", body="body"),
    )
    cfg = _sources().model_copy(update={"connectors": (*_sources().connectors, notes)})
    added = list_sources(cfg)[-1]
    assert added.agent_name == "notes" and added.query_tool == "query_notes" and added.update_tool is None
    assert not (ROOT / "src" / "nwb" / "connectors" / "notes.py").exists()

    writable = notes.model_copy(
        update={"writes": ("/sources/notes/**",), "update_action": "NOTES_CREATE", "update_kind": "create_note"}
    )
    spec = list_sources(_sources().model_copy(update={"connectors": (*_sources().connectors, writable)}))[-1]
    assert spec.update_tool == "update_notes" and spec.writes == ("/sources/notes/**",)


def test_query_statements_are_select_only() -> None:
    assert _SELECTS
    for sql in _SELECTS:
        assert sql.lstrip().upper().startswith("SELECT")
        assert _WRITE.search(sql) is None


def _slack(**update: Any) -> SourceSpec:
    spec = next(item for item in list_sources(_sources()) if item.id == "slack")
    assert spec.connector is not None
    return replace(spec, connector=spec.connector.model_copy(update=update))


def test_shipped_slack_entry_posts_nowhere_until_targets_are_configured() -> None:
    spec = _slack()
    assert spec.connector is not None and spec.connector.post_targets == ()
    sink = RecordingSink()
    out = _invoke(
        make_update_tool(spec, ConnectorBindings(sink=sink), gated=True),
        {"path": "/sources/slack/C0OUTBOX", "message": "hello"},
        _ctx(),
    )
    assert out.status == "error" and "allowed target" in str(out.content)
    assert sink.calls == []


def test_update_refuses_until_gated_and_on_an_allowed_path() -> None:
    spec = _slack(post_targets=("C0OUTBOX",))
    sink = RecordingSink()
    bindings = ConnectorBindings(sink=sink)
    allowed = {"path": "/sources/slack/C0OUTBOX", "message": "hello", "title": "note"}

    refused = _invoke(make_update_tool(spec, bindings, gated=False), allowed, _ctx())
    assert refused.status == "error" and "approval" in str(refused.content)
    assert sink.calls == []

    opened = make_update_tool(spec, bindings, gated=True)
    outside = _invoke(opened, {**allowed, "path": "/memories/USER.md"}, _ctx())
    assert outside.status == "error" and "allowlist" in str(outside.content)
    traversal = _invoke(opened, {**allowed, "path": "/sources/slack/../../memories/USER.md"}, _ctx())
    assert traversal.status == "error"
    other = _invoke(opened, {**allowed, "path": "/sources/slack/C0SOMEWHERE"}, _ctx())
    assert other.status == "error" and "allowed target" in str(other.content)
    assert sink.calls == []

    done = _invoke(opened, allowed, _ctx())
    assert done.status == "success" and done.artifact["external_ref"] == "ext-9"
    assert sink.calls == [("post_message", "C0OUTBOX", "note", "hello")]

    missing = _invoke(make_update_tool(spec, ConnectorBindings(), gated=True), allowed, _ctx())
    assert missing.status == "error" and "not connected" in str(missing.content)


class FakeSlackClient:
    """Records the call and answers like the action would. Nothing here reaches Slack."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._response = response

    def execute(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((action, dict(params)))
        return self._response


def test_approved_post_sends_the_action_arguments_and_returns_the_message_ts() -> None:
    spec = _slack(post_targets=("C0OUTBOX",))
    client = FakeSlackClient({"successful": True, "data": {"ok": True, "channel": "C0OUTBOX", "ts": "1726.0001"}})
    out = _invoke(
        make_update_tool(spec, ConnectorBindings(client=client), gated=True),
        {"path": "/sources/slack/C0OUTBOX", "message": "hello", "title": "note"},
        _ctx(),
    )
    assert out.status == "success" and out.artifact["external_ref"] == "1726.0001"
    assert client.calls == [("SLACK_SEND_MESSAGE", {"channel": "C0OUTBOX", "markdown_text": "hello"})]


def test_a_post_with_no_message_id_is_a_failure_not_done() -> None:
    spec = _slack(post_targets=("C0OUTBOX",))
    client = FakeSlackClient({"successful": False, "error": "channel_not_found", "data": {"ok": False}})
    out = _invoke(
        make_update_tool(spec, ConnectorBindings(client=client), gated=True),
        {"path": "/sources/slack/C0OUTBOX", "message": "hello"},
        _ctx(),
    )
    assert out.status == "error" and "no id" in str(out.content)
    assert out.artifact is None or out.artifact.get("status") != "done"
    assert len(client.calls) == 1


def test_writes_allowlist_fails_closed() -> None:
    assert path_allowed("/sources/slack/outbox/note.txt", ("/sources/slack/**",))
    assert not path_allowed("/memories/USER.md", ("/sources/slack/**",))
    assert not path_allowed("/sources/gmail/inbox/1.txt", ("/sources/slack/**",))
    assert not path_allowed("/sources/slack/outbox/note.txt", ())
    assert not path_allowed("/sources/slack/../../memories/USER.md", ("/sources/slack/**",))
