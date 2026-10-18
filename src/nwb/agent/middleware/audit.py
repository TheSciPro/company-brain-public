"""Every tool call -> a ``tool_calls`` receipt in SQL and a data-flow hop. Receipts survive summarisation.

Status comes from ``ToolMessage.status``; row counts from the structured ``artifact`` (tools use
``response_format="content_and_artifact"``); the side effect from the tool's metadata. No content parsing.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from nwb.agent.middleware._util import run_context
from nwb.agent.tools.base import side_effect_of
from nwb.db.repo_sessions import SessionRepository
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.ports import SqlDatabase

log = get_logger(__name__)
_LIST_FIELDS = ("hits", "pending", "candidates", "versions", "overdue")


def artifact_rows(artifact: Any) -> int | None:
    if isinstance(artifact, dict):
        for f in _LIST_FIELDS:
            if isinstance(artifact.get(f), list):
                return len(artifact[f])
    return None


class ToolAuditMiddleware(AgentMiddleware):
    def __init__(self, db: SqlDatabase) -> None:
        super().__init__()
        self._db = db

    @property
    def name(self) -> str:
        return "nwb_tool_audit"

    def _record(self, request: ToolCallRequest, result: ToolMessage | Command[Any], ms: int) -> None:
        ctx = run_context(request.runtime)
        name = str(request.tool_call.get("name"))
        args = dict(request.tool_call.get("args") or {})
        status, refusal, rows = "ok", None, None
        if isinstance(result, ToolMessage):
            if result.status == "error":
                status = "error"
                refusal = str(result.content)[:300]
            rows = artifact_rows(result.artifact)
        side = side_effect_of(request.tool)
        dataflow(Hop.TOOL_CALL, tool=name, args=args, status=status, ms=ms, side_effect=side, rows=rows)
        if ctx is not None:
            with self._db.unit_of_work(as_person=ctx.user_id, asof=ctx.asof_date) as uow:
                SessionRepository(uow).log_tool_call(
                    ctx.run_id, name, args, status=status, ms=ms, side_effect=side, result_rows=rows, refusal=refusal
                )

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]]
    ) -> ToolMessage | Command[Any]:
        t0 = time.perf_counter()
        result = handler(request)
        try:
            self._record(request, result, int((time.perf_counter() - t0) * 1000))
        except Exception:  # auditing must never break the user's turn; the failure itself is logged
            log.exception("tool audit failed for %s", request.tool_call.get("name"))
        return result

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]
    ) -> ToolMessage | Command[Any]:
        t0 = time.perf_counter()
        result = await handler(request)
        try:
            self._record(request, result, int((time.perf_counter() - t0) * 1000))
        except Exception:
            log.exception("tool audit failed for %s", request.tool_call.get("name"))
        return result
