"""Bootstrap note source for new hires (``BootstrapSource`` for ``BootstrapMiddleware``).

One beat from ``config/onboarding.yaml`` per successful chat turn, chosen by the pure state machine.
Runs whose purpose is not in ``onboard_purposes`` (heartbeat, cron, ...) get nothing and change nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from jinja2 import Environment, StrictUndefined, TemplateError

from nwb.agent.context import RuntimeContext
from nwb.conf.models import OnboardingConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.errors import PromptError
from nwb.obs.log import get_logger
from nwb.onboarding.flow import SHOWN, SKIPPED, Beat, FlowRecord, Shown, is_active, next_open, next_pending
from nwb.onboarding.service import OnboardingService
from nwb.ports import SqlDatabase
from nwb.prompts import PromptLibrary

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BootstrapNote:
    beat_id: str
    text: str


class OnboardingBootstrap:
    def __init__(
        self, db: SqlDatabase, onboarding: OnboardingService, prompts: PromptLibrary, prompt: str, cfg: OnboardingConfig
    ) -> None:
        self._db, self._ob, self._prompts, self._prompt, self._cfg = db, onboarding, prompts, prompt, cfg
        self._env = Environment(undefined=StrictUndefined, autoescape=False)  # noqa: S701 - prompt text, not HTML

    def note(self, ctx: RuntimeContext) -> BootstrapNote | None:
        if ctx.purpose not in self._cfg.onboard_purposes:
            return None
        plan = self._ob.plan(ctx.user_id)
        if plan is None or plan.welcomed:
            return None
        record, beats, _facts = self._ob.prepare_flow(ctx.user_id, ctx.asof_date)
        record = self._clear_unknown_waiting(ctx, record, beats)
        # Keep the beat that was asked on screen until it is answered or skipped. The next pending
        # beat is not published until then, so an answer cannot land on a beat the user has not seen.
        beat_id = _beat_to_offer(record, beats)
        if beat_id is None:
            if not plan.welcomed and record.phase != "onboarding":
                self._ob.save_flow(ctx.user_id, record)
            return None
        text = self._render(ctx, plan.buddy_id, beat_id)
        if text is None:
            return self._offer_next(ctx, plan.buddy_id, record, beats, beat_id)
        return BootstrapNote(beat_id, text)

    def offered(self, ctx: RuntimeContext, beat_id: str) -> None:
        """The beat was actually shown on a run that finished. A failed run never gets here."""
        if ctx.purpose not in self._cfg.onboard_purposes:
            return
        self._ob.apply_event(ctx.user_id, ctx.asof_date, Shown(beat_id), ctx.purpose)

    def _render(self, ctx: RuntimeContext, buddy_id: str | None, beat_id: str) -> str | None:
        asof = ctx.asof_date
        plan = self._ob.plan(ctx.user_id)
        with self._db.unit_of_work() as uow:
            directory = DirectoryReader(uow)
            me = directory.person(ctx.user_id)
            mgr = directory.manager(ctx.user_id, asof)
            buddy = directory.person(buddy_id) if buddy_id else None
            units = [m.unit_name for m in directory.memberships(ctx.user_id, asof)]
        overdue, upcoming = self._ob.open_items(ctx.user_id, asof)
        top = [f"{item.title} (day {item.due_day})" for item in (overdue + upcoming)[: self._cfg.bootstrap.top_items]]
        values: dict[str, object] = {
            "name": me.name if me else ctx.user_id,
            "day": self._ob.day(plan, asof) if plan else 0,
            "team": ", ".join(units) or "your team",
            "manager": f"{mgr.name} ({mgr.id})" if mgr else "your manager",
            "buddy": f"{buddy.name} ({buddy.id})" if buddy else "",
            "top_items": top,
        }
        spec = next((item for item in self._cfg.beats if item.id == beat_id), None)
        if spec is None:
            log.warning("onboarding beat %s is no longer in config", beat_id, person=ctx.user_id, beat=beat_id)
            return None
        try:
            beat_text = self._env.from_string(spec.text).render(**values).strip()
            example = self._env.from_string(spec.example).render(**values).strip() if spec.example else ""
        except TemplateError as exc:
            raise PromptError(f"onboarding beat {beat_id} failed to render: {exc}") from exc
        return self._prompts.render(self._prompt, **values, beat_id=beat_id, beat_text=beat_text, example=example).text

    def _clear_unknown_waiting(self, ctx: RuntimeContext, record: FlowRecord, beats: Sequence[Beat]) -> FlowRecord:
        """A shown beat removed from config is not on screen. Clear it and keep the chat moving."""
        waiting = record.waiting
        if waiting is None or any(beat.id == waiting for beat in beats):
            return record
        log.warning("onboarding beat %s is no longer in config; clearing it", waiting, person=ctx.user_id, beat=waiting)
        cleared = record.model_copy(update={"waiting": None})
        self._ob.save_flow(ctx.user_id, cleared)
        return cleared

    def _offer_next(
        self, ctx: RuntimeContext, buddy_id: str | None, record: FlowRecord, beats: Sequence[Beat], skipped: str
    ) -> BootstrapNote | None:
        """The chosen id is not in config. Offer the next beat that still is."""
        statuses = dict(record.beats)
        if statuses.get(skipped) == SHOWN:
            statuses[skipped] = SKIPPED
        cleared = record.model_copy(update={"waiting": None, "beats": statuses})
        self._ob.save_flow(ctx.user_id, cleared)
        nxt = _beat_to_offer(cleared, beats)
        if nxt is None or nxt == skipped:
            return None
        text = self._render(ctx, buddy_id, nxt)
        if text is None:
            return None
        return BootstrapNote(nxt, text)


def _beat_to_offer(record: FlowRecord, beats: Sequence[Beat]) -> str | None:
    on_screen = next_open(record, beats)
    if on_screen is not None:
        return on_screen
    if is_active(record):
        return next_pending(record, beats)
    return None
