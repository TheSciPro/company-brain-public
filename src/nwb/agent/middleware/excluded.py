"""Hard refusal of excluded built-in tools at execution time.

deepagents' ``HarnessProfile(excluded_tools=...)`` hides tools from the MODEL (its ``_ToolExclusionMiddleware``
filters the bound tools), but the tool node still holds them: a hallucinated ``delete`` call would run. This
middleware closes that gap for the main agent and every subagent: an excluded name never reaches its handler.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class ExcludedToolsMiddleware(AgentMiddleware):
    def __init__(self, excluded: frozenset[str]) -> None:
        super().__init__()
        self._excluded = excluded

    @property
    def name(self) -> str:
        return "nwb_excluded_tools"

    def _refuse(self, request: ToolCallRequest) -> ToolMessage | None:
        name = str(request.tool_call.get("name"))
        if name not in self._excluded:
            return None
        return ToolMessage(
            content=f"Error: the {name} tool is forbidden in this deployment.",
            name=name,
            tool_call_id=str(request.tool_call.get("id")),
            status="error",
        )

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
    ) -> ToolMessage | Command[Any]:
        return self._refuse(request) or handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        return self._refuse(request) or await handler(request)
