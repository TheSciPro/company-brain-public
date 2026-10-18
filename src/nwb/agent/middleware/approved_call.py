"""Execute a human-approved skill tool with the reviewer's arguments.

Skill tools are not on the tool node until their skill has been read. ``interrupt_on``
still pauses on the name, and a person may edit the call before it has ever run.
Batched HITL (deepagents' default) resumes with
``{"decisions": [{"type": "edit", "edited_action": {"name", "args"}}]}`` and stores
that replacement on ``hitl_edited_tool_calls`` for its own wrapper to apply. This
middleware applies the same replacement first, and attaches the tool, so the audit
receipt and the execution both see the edited arguments even when the skill was
never read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from nwb.obs.log import get_logger

log = get_logger(__name__)

# LangChain HumanInTheLoopMiddleware, batched mode. Not part of its public API;
# the resume payload above is. If the key moves, the library wrapper still
# substitutes at execution time once the tool is attached.
HITL_EDITED_CALLS = "hitl_edited_tool_calls"


class ApprovedCallMiddleware(AgentMiddleware):
    def __init__(self, tools: Mapping[str, BaseTool], interrupt_on: frozenset[str]) -> None:
        super().__init__()
        self._tools = dict(tools)
        self._interrupt = interrupt_on

    @property
    def name(self) -> str:
        return "nwb_approved_call"

    def _prepare(self, request: ToolCallRequest) -> ToolCallRequest:
        edited = _edited_action(request)
        name = str(request.tool_call.get("name") or "")
        args = request.tool_call.get("args")
        if edited is not None:
            renamed = edited.get("name")
            if isinstance(renamed, str) and renamed:
                name = renamed
            replacement_args = edited.get("args")
            if isinstance(replacement_args, dict):
                args = replacement_args
        tool = request.tool
        if tool is None or getattr(tool, "name", None) != name:
            found = self._tools.get(name)
            if found is not None and (name in self._interrupt or edited is not None):
                tool = found
            elif tool is None and name in self._interrupt:
                log.warning("approved call has no tool implementation", tool=name)
        same_name = name == request.tool_call.get("name")
        same_args = args is request.tool_call.get("args")
        same_tool = tool is request.tool
        if same_name and same_args and same_tool:
            return request
        call = {**request.tool_call, "name": name, "args": args if isinstance(args, dict) else {}}
        if tool is not request.tool:
            return request.override(tool_call=call, tool=tool)
        return request.override(tool_call=call)

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
    ) -> ToolMessage | Command[Any]:
        return handler(self._prepare(request))

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        return await handler(self._prepare(request))


def _edited_action(request: ToolCallRequest) -> Mapping[str, Any] | None:
    state = request.state
    if not isinstance(state, Mapping):
        return None
    edits = state.get(HITL_EDITED_CALLS)
    if not isinstance(edits, Mapping):
        return None
    call_id = request.tool_call.get("id")
    if not call_id:
        return None
    edited = edits.get(call_id)
    return edited if isinstance(edited, Mapping) else None
