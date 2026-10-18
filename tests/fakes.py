"""Test doubles: a scripted chat model that supports tool calling, and helpers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

Script = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """Replays a list of responses; a callable entry sees the prompt (useful to assert on payload)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    script: list[Any] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        self.bound_tools = [getattr(t, "name", None) or t.get("name", "?") for t in tools]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        if not self.script:
            msg = AIMessage(content="(script exhausted)")
        else:
            nxt = self.script.pop(0)
            msg = nxt(messages) if callable(nxt) else nxt
        return ChatResult(generations=[ChatGeneration(message=msg)])


def call(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
    """An AIMessage that requests one tool call."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])
