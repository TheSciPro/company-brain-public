"""Helpers shared by the middleware modules."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage

from nwb.agent.context import RuntimeContext


def run_context(runtime: Any) -> RuntimeContext | None:
    ctx = getattr(runtime, "context", None)
    return ctx if isinstance(ctx, RuntimeContext) else None


def last_user_text(messages: Sequence[AnyMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def append_system(request: ModelRequest[Any], text: str) -> ModelRequest[Any]:
    """Dynamic text goes LAST in the system message so the static prefix (and the provider cache) survives."""
    if not text:
        return request
    base = request.system_message.content if request.system_message is not None else ""
    base_text = (
        base if isinstance(base, str) else "\n".join(str(b.get("text", "")) for b in base if isinstance(b, dict))
    )
    return request.override(system_message=SystemMessage(content=f"{base_text}\n\n{text}"))


def messages_this_run(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Messages after the latest user message (what this run produced)."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return list(messages[i + 1 :])
    return list(messages)


def state_of(request: ModelRequest[Any]) -> dict[str, Any]:
    st = request.state
    return dict(st) if isinstance(st, dict) else {}
