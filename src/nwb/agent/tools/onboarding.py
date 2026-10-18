"""Onboarding tools: the new joiner's day-N brief (checklist generated from their data) and item progress."""

from __future__ import annotations

from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from nwb.agent.tools.base import Result, context, domain_errors, finish, result
from nwb.onboarding.flow import Answered, End, Skipped, next_open
from nwb.onboarding.models import ItemStatus, OnboardingStep
from nwb.onboarding.service import OnboardingService


class OnboardingTools:
    def __init__(self, onboarding: OnboardingService) -> None:
        self._ob = onboarding

    def tools(self) -> list[BaseTool]:
        ob = self._ob

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def onboarding_brief(
            runtime: ToolRuntime[Any, Any], focus: Literal["all", "overdue", "upcoming"] = "all"
        ) -> Result:
            """The current user's onboarding brief: day N, phase, manager, buddy, overdue and upcoming items.

            Items are verified against source systems first (trainings, acknowledgements, access), so done work
            is not reported as overdue.

            Args:
                focus: narrow the item lists to overdue or upcoming.
            """
            ctx = context(runtime)
            brief = ob.brief(ctx.user_id, ctx.asof_date)
            if focus == "overdue":
                brief = brief.model_copy(update={"upcoming": []})
            elif focus == "upcoming":
                brief = brief.model_copy(update={"overdue": []})
            return result(brief)

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def update_onboarding_item(
            item_id: str, status: ItemStatus, runtime: ToolRuntime[Any, Any], evidence: str | None = None
        ) -> Result:
            """Record progress on one of the user's onboarding items (when they say it is done, blocked, ...).

            Args:
                item_id: item id from onboarding_brief.
                status: todo, doing, done, waived or blocked.
                evidence: what the user said proves it, optional.
            """
            ctx = context(runtime)
            return result(ob.update_item(ctx.user_id, item_id, status, evidence))

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def onboarding_step(
            action: Literal["answer", "skip", "skip_all"],
            runtime: ToolRuntime[Any, Any],
            beat: str | None = None,
            note: str | None = None,
        ) -> Result:
            """Record the user's answer or skip for the onboarding beat they were just shown.

            The user's own request comes first. Call this only after answering them. skip_all ends the ritual.
            The answer is stored on the beat that was asked. A different id is ignored.

            Args:
                action: answer, skip, or skip_all.
                beat: beat id from the onboarding note. The server uses the beat that was asked.
                note: what the user said. Required when action is answer.
            """
            ctx = context(runtime)
            del beat  # a model-supplied id must not move the answer onto a beat that was not asked
            if action == "skip_all" or not ob.allows(ctx.purpose):
                return result(ob.apply_event(ctx.user_id, ctx.asof_date, End(), ctx.purpose))
            record, beats, _facts = ob.prepare_flow(ctx.user_id, ctx.asof_date)
            asked = next_open(record, beats)
            if asked is None:
                return result(OnboardingStep(phase=record.phase, error="no onboarding beat has been asked yet"))
            event: Skipped | Answered = Skipped(asked) if action == "skip" else Answered(asked, note or "")
            return result(ob.apply_event(ctx.user_id, ctx.asof_date, event, ctx.purpose))

        return [
            finish(onboarding_brief),
            finish(update_onboarding_item, "write_memory"),
            finish(onboarding_step, "write_memory"),
        ]
