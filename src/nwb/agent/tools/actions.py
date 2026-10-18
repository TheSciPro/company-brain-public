"""Action tools: propose an action into the approval queue (HITL via ``interrupt_on``); list my approvals.

``propose_action`` is in ``interrupt_on`` (config/agent.yaml): LangGraph pauses BEFORE it runs and the human
approves or rejects in chat. Deciding approvals is deliberately not a tool: humans decide via API, CLI or UI.
"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from nwb.actions.models import ActionView
from nwb.actions.service import ActionService
from nwb.agent.tools.base import Result, context, domain_errors, finish, result


class ApprovalsOut(BaseModel):
    pending: list[ActionView]


class ActionTools:
    def __init__(self, actions: ActionService, hitl_gated: bool) -> None:
        self._actions = actions
        self._gated = hitl_gated

    def tools(self) -> list[BaseTool]:
        actions, gated = self._actions, self._gated

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def propose_action(
            kind: str, title: str, message: str, runtime: ToolRuntime[Any, Any], target_person_id: str | None = None
        ) -> Result:
            """Propose an action outside the chat (e.g. a reminder to a colleague). The user approves it first.

            Args:
                kind: an action kind from the actions skill.
                title: short title the approver sees.
                message: the message the action delivers.
                target_person_id: id of the person the action is about, if any.
            """
            ctx = context(runtime)
            return result(
                actions.propose(
                    kind_name=kind,
                    requester=ctx.user_id,
                    target=target_person_id,
                    title=title,
                    args={"message": message},
                    asof=ctx.asof_date,
                    proposed_by="agent",
                    run_id=ctx.run_id,
                    pre_approved=gated,
                )
            )

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def my_approvals(runtime: ToolRuntime[Any, Any], limit: int = 10) -> Result:
            """Approvals waiting on the current user (read-only).

            Args:
                limit: max items.
            """
            return result(ApprovalsOut(pending=actions.pending_for(context(runtime).user_id)[:limit]))

        return [finish(propose_action, "write_action"), finish(my_approvals)]
