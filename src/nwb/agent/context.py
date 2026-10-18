"""RuntimeContext: the immutable per-run context (LangGraph ``context_schema``). Set by the runner, never by the model.

Tools, middleware and the ``/memories/`` store namespace read identity from ``runtime.context`` only (AGENTS 5.5
rule 1): no tool schema has a user, company or date argument. ``purpose`` says why the agent is running, so the
same graph serves chat turns, scheduled heartbeat turns and background memory consolidation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from langgraph.runtime import Runtime

from nwb.identity import Actor
from nwb.purpose import PURPOSES, Purpose

__all__ = ["PURPOSES", "Purpose", "RuntimeContext"]


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    company_id: str
    user_id: str
    session_id: str  # also the LangGraph thread_id: one conversation = one thread
    run_id: str
    asof: str  # ISO date the user is asking "as of"
    message_id: str = ""  # the user message that started this run ("" for scheduled runs)
    purpose: Purpose = "chat"

    @property
    def asof_date(self) -> date:
        return date.fromisoformat(self.asof)

    @property
    def is_chat(self) -> bool:
        return self.purpose == "chat"

    def actor(self) -> Actor:
        return Actor(
            company_id=self.company_id,
            person_id=self.user_id,
            asof=self.asof_date,
            session_id=self.session_id,
            message_id=self.message_id or None,
            run_id=self.run_id,
        )

    def memory_namespace(self) -> tuple[str, str, str]:
        """Store namespace for this user's ``/memories/`` files: isolated per (company, user)."""
        return (self.company_id, self.user_id, "memories")


def user_namespace(runtime: Runtime[Any]) -> tuple[str, ...]:
    """Store namespace for the current run. Refuses to guess when the runtime has no ``RuntimeContext``."""
    ctx = runtime.context
    if not isinstance(ctx, RuntimeContext):
        raise PermissionError("/memories/ needs a RuntimeContext; refusing to guess whose files these are")
    return ctx.memory_namespace()
