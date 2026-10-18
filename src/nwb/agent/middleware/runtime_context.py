"""Memorae v3 ``<runtime>`` block, computed once per run in ``before_agent`` and kept in graph state.

Sections: temporal (the user's as-of date, relative dates), account (people data = "the heart"), access summary,
guessed memories (pre-retrieved by similarity, marked as hints). It deliberately holds NO conversation history
(the checkpointer has it) and NO profile (``/memories/USER.md`` is loaded by deepagents' ``MemoryMiddleware``).
"""

from __future__ import annotations

import calendar
import time
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain.agents.middleware.types import PrivateStateAttr

from nwb.agent.clock import Clock
from nwb.agent.context import RuntimeContext
from nwb.agent.middleware._util import append_system, last_user_text, run_context, state_of
from nwb.conf.models import PayloadConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.memory.store import PersonalMemory
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.ports import SqlDatabase
from nwb.prompts import PromptLibrary

log = get_logger(__name__)


class RuntimeState(AgentState):
    nwb_runtime: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]


def temporal_block(time_hm: str, tz: str, asof: date, next_days: int) -> dict[str, Any]:
    """'Today' is the user's as-of date (a demo persona may live on another day than the wall clock)."""
    days_to_monday = (7 - asof.weekday()) % 7 or 7
    last = calendar.monthrange(asof.year, asof.month)[1]
    return {
        "time": time_hm,
        "weekday": asof.strftime("%A"),
        "tz": tz,
        "asof": asof.isoformat(),
        "next_days": [(asof + timedelta(days=i)).strftime("%a %d %b") for i in range(1, next_days + 1)],
        "tomorrow": (asof + timedelta(days=1)).isoformat(),
        "next_monday": (asof + timedelta(days=days_to_monday)).isoformat(),
        "end_of_month": asof.replace(day=last).isoformat(),
    }


class RuntimeBlockBuilder:
    """Pure SQL + one memory search; no LLM. Logged as a data-flow hop with its prompt version."""

    def __init__(
        self,
        db: SqlDatabase,
        memory: PersonalMemory,
        prompts: PromptLibrary,
        cfg: PayloadConfig,
        clock: Clock,
        timezone: str,
        company_name: str,
        prompt_name: str,
    ) -> None:
        self._db, self._memory, self._prompts = db, memory, prompts
        self._cfg, self._clock, self._tz = cfg, clock, timezone
        self._company, self._prompt = company_name, prompt_name

    def build(self, ctx: RuntimeContext, user_text: str) -> tuple[str, str]:
        t0 = time.perf_counter()
        asof = ctx.asof_date
        with self._db.unit_of_work(as_person=ctx.user_id, asof=asof) as uow:
            d = DirectoryReader(uow)
            person = d.person(ctx.user_id)
            units = d.memberships(ctx.user_id, asof)
            manager = d.manager(ctx.user_id, asof)
            access = d.access_summary(ctx.user_id, asof) if self._cfg.show_access_summary else None
        if person is None:
            raise LookupError(f"person {ctx.user_id} not in directory")
        guessed: list[dict[str, Any]] = []
        not_searched: list[str] = []
        if self._cfg.guessed_memories_top_k and user_text.strip() and ctx.is_chat:
            res = self._memory.recall(ctx.actor(), user_text, {ctx.user_id}, self._cfg.guessed_memories_top_k)
            not_searched = list(res.not_searched)
            guessed = [
                {
                    "id": h.fact.id,
                    "score": h.score,
                    "kind": h.fact.kind,
                    "date": h.fact.valid_at.date().isoformat() if h.fact.valid_at else "",
                    "text": _clip(h.fact.text, 200),
                }
                for h in res.hits
                if h.score >= self._cfg.guessed_memories_min_score
            ]
        account = {
            "person_id": person.id,
            "name": person.name,
            "title": person.title,
            "grade": person.grade,
            "location": person.location,
            "country": person.country,
            "company": self._company,
            "units": [f"{u.unit_name}{' (lead)' if u.is_leader else ''}" for u in units],
            "manager": f"{manager.name} ({manager.id})" if manager else None,
        }
        access_text = ""
        if access is not None:
            expiry = f" (next expiry {access.next_grant_expiry})" if access.next_grant_expiry else ""
            access_text = (
                f"groups: {', '.join(access.groups) or '-'} | personal grants: {access.grant_count}{expiry} | "
                f"leads: {', '.join(access.leads_units) or '-'}"
            )
        rendered = self._prompts.render(
            self._prompt,
            temporal=temporal_block(self._clock.now().strftime("%H:%M"), self._tz, asof, self._cfg.next_days),
            account=account,
            access=access_text,
            guessed=guessed,
            not_searched=not_searched,
            purpose=ctx.purpose,
        )
        dataflow(
            Hop.PAYLOAD,
            person=ctx.user_id,
            run=ctx.run_id,
            prompt=rendered.ref,
            chars=len(rendered.text),
            guessed=len(guessed),
            ms=int((time.perf_counter() - t0) * 1000),
            text=rendered.text,
        )
        return rendered.text, rendered.ref


class RuntimeContextMiddleware(AgentMiddleware[RuntimeState, RuntimeContext, Any]):
    state_schema = RuntimeState

    def __init__(self, builder: RuntimeBlockBuilder) -> None:
        super().__init__()
        self._builder = builder

    @property
    def name(self) -> str:
        return "nwb_runtime_context"

    def before_agent(self, state: RuntimeState, runtime: Any) -> dict[str, Any] | None:
        ctx = run_context(runtime)
        if ctx is None:
            return None
        text, ref = self._builder.build(ctx, last_user_text(state["messages"]))
        log.debug("context built", run=ctx.run_id, user=ctx.user_id, asof=ctx.asof, prompt=ref, chars=len(text))
        return {"nwb_runtime": {"run": ctx.run_id, "text": text, "ref": ref}}

    def _apply(self, request: ModelRequest[RuntimeContext]) -> ModelRequest[RuntimeContext]:
        block = state_of(request).get("nwb_runtime") or {}
        ctx = run_context(request.runtime)
        if ctx is None or block.get("run") != ctx.run_id:
            return request
        return append_system(request, block.get("text", ""))

    def wrap_model_call(
        self,
        request: ModelRequest[RuntimeContext],
        handler: Callable[[ModelRequest[RuntimeContext]], ModelResponse[Any]],
    ) -> ModelResponse:
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[RuntimeContext],
        handler: Callable[[ModelRequest[RuntimeContext]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._apply(request))


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"
