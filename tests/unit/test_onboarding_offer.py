"""A beat is published by the final reply only. A tool-call step's marker is not an offer."""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from nwb.agent.middleware.bootstrap import _scrub_message, beat_landed

BEAT = "confirm_place"
MARKER = f'<onboarding-beat id="{BEAT}"/>'


def _step(text: str) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=[{"name": "search_memory", "args": {"query": "manager"}, "id": "c1", "type": "tool_call"}],
    )


def test_a_marker_on_a_tool_call_step_does_not_publish_the_beat() -> None:
    step = _scrub_message(_step(f"Looking that up.\n{MARKER}"), BEAT)
    final = _scrub_message(AIMessage(content="Your manager is NW2075."), BEAT)
    produced = [step, ToolMessage(content="ok", tool_call_id="c1", name="search_memory"), final]
    assert step.additional_kwargs.get("nwb_offered_beat") != BEAT
    assert "<onboarding-beat" not in str(step.content)
    assert not beat_landed(produced, BEAT)
    assert not beat_landed([_step(f"Looking that up.\n{MARKER}")], BEAT)
    reply = _scrub_message(AIMessage(content=f"Does that match?\n{MARKER}"), BEAT)
    assert reply.additional_kwargs.get("nwb_offered_beat") == BEAT
    assert beat_landed([step, reply], BEAT)
