"""Source subagents on the loaded company: one per catalog entry, read-only queries, approval on update."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage

from nwb.agent.backends import WritePolicyBackend
from nwb.agent.context import RuntimeContext
from nwb.agent.subagents import SubAgent
from tests.agent.conftest import Harness
from tests.fakes import call

IDENTITY = {"user_id", "person_id", "owner_id", "asof", "company_id", "session_id", "runtime", "requester"}


def _runtime(ctx: Any) -> ToolRuntime[Any, Any]:
    return ToolRuntime(state={}, context=ctx, config={}, stream_writer=lambda _: None, tool_call_id="t", store=None)


def _invoke(tool: Any, args: dict[str, Any], ctx: Any) -> ToolMessage:
    out = tool.invoke({"type": "tool_call", "id": "t", "name": tool.name, "args": {**args, "runtime": _runtime(ctx)}})
    assert isinstance(out, ToolMessage)
    return out


def _filesystem(spec: SubAgent) -> FilesystemMiddleware:
    found = [m for m in spec["middleware"] if isinstance(m, FilesystemMiddleware)]
    assert len(found) == 1
    assert isinstance(found[0].backend, WritePolicyBackend)
    return found[0]


class RecordingSink:
    name = "test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def execute(self, kind: str, target_ref: str, title: str, message: str) -> str:
        self.calls.append((kind, target_ref, title, message))
        return "ext-9"


class FakeSlack:
    """Stands in for the connected app. Nothing here reaches Slack."""

    def __init__(self, messages: list[dict[str, str]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._messages = messages

    def execute(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((action, dict(params)))
        return {"messages": self._messages}


def _slack_config(harness: Harness, **update: Any) -> None:
    """Configure the Slack entry for this test only. Must run before ``source_agents()`` is built."""
    cfg = harness.c.sources
    connectors = tuple(item.model_copy(update=update) if item.id == "slack" else item for item in cfg.connectors)
    harness.c.__dict__["sources"] = cfg.model_copy(update={"connectors": connectors})


def test_built_subagents_match_the_catalog_and_use_the_background_model(harness: Harness) -> None:
    built = harness.c.source_agents()
    assert [spec.agent_name for spec in built.specs] == [sub["name"] for sub in built.subagents]
    assert all(sub["model"] is harness.c.models.background for sub in built.subagents)
    assert set(built.interrupts) == {"update_slack"}
    by_name = {sub["name"]: sub for sub in built.subagents}
    assert "update_slack" in by_name["slack"]["interrupt_on"]
    assert "interrupt_on" not in by_name["policies"]
    assert "write_file" not in {tool.name for tool in _filesystem(by_name["policies"]).tools}
    denied = _filesystem(by_name["slack"]).backend.write("/memories/USER.md", "nope")
    assert denied.error and "permission denied" in denied.error
    # Assembling the chat agent must accept the source subagents (unique names, interrupts).
    assert harness.c.agent is not None


def test_source_tool_schemas_carry_no_identity(harness: Harness) -> None:
    tools = harness.c.source_agents().tools
    assert "query_policies" in tools and "update_slack" in tools
    assert "update_gmail" not in tools
    for name, tool in tools.items():
        props = set(tool.tool_call_schema.model_json_schema().get("properties", {}))
        assert not props & IDENTITY, name
        effect = (tool.metadata or {}).get("side_effect")
        assert effect == ("external" if name.startswith("update_") else "none")


def test_query_policies_returns_a_visible_document_and_hides_the_rest(harness: Harness) -> None:
    tool = harness.c.source_agents().tools["query_policies"]
    out = _invoke(tool, {"question": "hybrid work home policy"}, harness.ctx())
    assert out.status == "success"
    assert "POL-HR-WFH@v3" in [hit["id"] for hit in out.artifact["hits"]]
    hidden = _invoke(tool, {"question": "hybrid work home policy"}, harness.ctx("NW-NOBODY"))
    assert hidden.artifact["hits"] == []


def test_pricing_records_follow_the_access_policy(harness: Harness) -> None:
    tool = harness.c.source_agents().tools["query_pricing_exceptions"]
    nikhil = _invoke(tool, {"question": "brightmart discount"}, harness.ctx("NW1103"))
    aditi = _invoke(tool, {"question": "brightmart discount"}, harness.ctx("NW0201"))
    assert not any("PEX-2026-004" in hit["id"] for hit in nikhil.artifact["hits"])
    assert "18" not in str(nikhil.artifact)
    assert any("PEX-2026-004" in hit["id"] for hit in aditi.artifact["hits"])


def test_main_agent_delegates_a_source_question(harness: Harness) -> None:
    harness.model.script = [
        call(
            "task",
            {"description": "How many days a week does the work-from-home policy allow?", "subagent_type": "policies"},
            "t1",
        ),
        call("query_policies", {"question": "tue thu anchor days"}, "q1"),
        AIMessage(content="Up to 3 days a week [POL-HR-WFH@v3]."),
        AIMessage(content="The policy allows up to 3 days a week [POL-HR-WFH@v3]."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", harness.actor().asof)
    events = list(svc.stream("NW1103", sid, "How many days a week can I work from home?"))
    ends = [e for e in events if e.type == "tool_end" and e.data.get("tool") == "query_policies"]
    assert ends and ends[0].data["status"] == "success"
    assert "POL-HR-WFH@v3" in ends[0].data["preview"]
    final = next(e for e in events if e.type == "final")
    assert final.data["status"] == "done" and "POL-HR-WFH@v3" in final.data["answer"]


def test_update_pauses_for_approval_and_runs_only_after_it(harness: Harness) -> None:
    _slack_config(harness, post_targets=("C0OUTBOX",))
    sink = RecordingSink()
    harness.c.connector_bindings.sink = sink
    harness.model.script = [
        call(
            "task",
            {"description": "Post hello to /sources/slack/C0OUTBOX", "subagent_type": "slack"},
            "t1",
        ),
        call(
            "update_slack",
            {"path": "/sources/slack/C0OUTBOX", "message": "hello", "title": "note"},
            "u1",
        ),
        AIMessage(content="Posted hello."),
        AIMessage(content="Posted hello after approval."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", harness.actor().asof)
    paused = svc.turn("NW1103", sid, "post hello to the slack outbox")
    assert paused.status == "interrupted" and paused.interrupt
    req = paused.interrupt["interrupts"][0]["action_requests"][0]
    assert req["name"] == "update_slack"
    assert req["args"]["path"] == "/sources/slack/C0OUTBOX"
    assert sink.calls == []
    events = list(svc.resume("NW1103", paused.run_id, [{"type": "approve"}]))
    assert next(e for e in events if e.type == "final").data["status"] == "done"
    assert sink.calls == [("post_message", "C0OUTBOX", "note", "hello")]


def test_rejected_update_does_not_run(harness: Harness) -> None:
    _slack_config(harness, post_targets=("C0OUTBOX",))
    sink = RecordingSink()
    harness.c.connector_bindings.sink = sink
    harness.model.script = [
        call(
            "task",
            {"description": "Post hello to /sources/slack/C0OUTBOX", "subagent_type": "slack"},
            "t1",
        ),
        call(
            "update_slack",
            {"path": "/sources/slack/C0OUTBOX", "message": "hello", "title": "note"},
            "u1",
        ),
        AIMessage(content="I did not post it."),
        AIMessage(content="I did not post it."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", harness.actor().asof)
    paused = svc.turn("NW1103", sid, "post hello to the slack outbox")
    assert paused.status == "interrupted"
    events = list(svc.resume("NW1103", paused.run_id, [{"type": "reject", "message": "no"}]))
    assert next(e for e in events if e.type == "final").data["status"] == "done"
    assert sink.calls == []


def _at(harness: Harness, person: str, asof: str) -> RuntimeContext:
    return RuntimeContext(harness.c.settings.company_id, person, "s-test", "run-test", asof, "")


def test_directory_needs_a_current_membership_and_lists_only_current_people(harness: Harness) -> None:
    company = harness.c.settings.company_id
    with harness.c.db.unit_of_work() as uow:
        unit = uow.fetchone("SELECT id FROM org_units ORDER BY id")
        assert unit is not None
        uow.execute(
            "INSERT INTO people (id, company_id, name, status) VALUES ('NW-GONE', :co, 'Zephyr Quillfeather', 'active')",
            {"co": company},
        )
        uow.execute(
            "INSERT INTO memberships (id, person_id, unit_id, valid_from, valid_to) "
            "VALUES ('m-gone', 'NW-GONE', :unit, '2020-01-01', '2021-01-01')",
            {"unit": unit["id"]},
        )
        row = uow.fetchone("SELECT name FROM people WHERE id = 'NW1103'")
        assert row is not None
    tool = harness.c.source_agents().tools["query_directory"]
    own = str(row["name"])
    found = _invoke(tool, {"question": own}, harness.ctx("NW1103"))
    assert "NW1103" in [hit["id"] for hit in found.artifact["hits"]]
    # Nobody without a membership on the ask date gets the roster.
    assert _invoke(tool, {"question": own}, harness.ctx("NW-NOBODY")).artifact["hits"] == []
    assert _invoke(tool, {"question": own}, _at(harness, "NW1103", "1990-01-01")).artifact["hits"] == []
    # A person whose membership ended is not listed on a later date, and is while it was current.
    assert _invoke(tool, {"question": "Zephyr Quillfeather"}, harness.ctx("NW1103")).artifact["hits"] == []
    assert _invoke(tool, {"question": "Zephyr Quillfeather"}, _at(harness, "NW-GONE", "2020-06-01")).artifact[
        "hits"
    ] == [{"id": "NW-GONE", "title": "Zephyr Quillfeather", "snippet": "", "source": "directory"}]


def test_slack_reads_the_configured_channel_and_maps_users_through_identities(harness: Harness) -> None:
    with harness.c.db.unit_of_work() as uow:
        uow.execute(
            "INSERT INTO user_identities (id, person_id, provider, subject) VALUES ('ui-1', 'NW1103', 'slack', 'U0NIKHIL')"
        )
    slack = FakeSlack(
        [
            {"ts": "1726800000.000100", "user": "U0NIKHIL", "text": "brightmart bid deadline is friday"},
            {"ts": "1726800000.000200", "user": "U0SOMEONE", "text": "brightmart bid margin note"},
            {"ts": "1726800000.000300", "user": "NW1103", "text": "brightmart bid with our id as the user"},
        ]
    )
    harness.c.connector_bindings.client = slack
    _slack_config(harness, channels=("C0BID",))
    tool = harness.c.source_agents().tools["query_slack"]
    out = _invoke(tool, {"question": "brightmart bid"}, harness.ctx("NW1103"))
    assert [hit["id"] for hit in out.artifact["hits"]] == ["1726800000.000100"]
    assert slack.calls == [("SLACK_FETCH_CONVERSATION_HISTORY", {"limit": 50, "channel": "C0BID"})]
    other = _invoke(tool, {"question": "brightmart bid"}, harness.ctx("NW0201"))
    assert other.artifact["hits"] == []


def test_slack_without_configured_channels_reads_nothing(harness: Harness) -> None:
    slack = FakeSlack([{"ts": "1", "user": "U0NIKHIL", "text": "brightmart"}])
    harness.c.connector_bindings.client = slack
    out = _invoke(harness.c.source_agents().tools["query_slack"], {"question": "brightmart"}, harness.ctx("NW1103"))
    assert out.artifact["hits"] == [] and slack.calls == []


def test_source_subagent_can_list_its_scratch_files(harness: Harness) -> None:
    """A source subagent's ``ls /`` reads its own ``files`` state; it must not crash the run (KeyError 'files')."""
    harness.model.script = [
        call("task", {"description": "Find the WFH policy.", "subagent_type": "policies"}, "t1"),
        call("ls", {"path": "/"}, "l1"),
        AIMessage(content="Nothing in scratch."),
        AIMessage(content="I could not find it."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", harness.actor().asof)
    events = list(svc.stream("NW1103", sid, "What is the WFH policy?"))
    assert not [e for e in events if e.type == "error"], [e.data for e in events if e.type == "error"]
    ls_end = next(e for e in events if e.type == "tool_end" and e.data.get("tool") == "ls")
    assert ls_end.data["status"] == "success"
