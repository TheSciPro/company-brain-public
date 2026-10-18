"""Onboarding service: creates plans from templates, computes the day-N brief, records item progress.

Plans are data (config/onboarding.yaml -> onboarding_templates -> onboarding_items). The brief reads the graph of
people (manager/buddy as of the date). Policy questions go through company_fact (scope + precedence apply).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from nwb.conf.models import OnboardingBeat, OnboardingConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.db.repo_onboarding import ItemRow, OnboardingRepository, PlanRow
from nwb.db.repo_sessions import new_id
from nwb.errors import NotFoundError, NwbError
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.onboarding.flow import (
    ONBOARDING,
    Atom,
    Beat,
    Event,
    FlowRecord,
    When,
    align,
    apply,
    new_record,
)
from nwb.onboarding.models import BriefItem, ItemStatus, ItemUpdate, OnboardingBrief, OnboardingStep
from nwb.ports import SqlDatabase, UnitOfWork

log = get_logger(__name__)


def _grade(g: object) -> int:
    m = re.search(r"\d+", str(g or ""))
    return int(m.group()) if m else -1


class OnboardingService:
    def __init__(
        self,
        db: SqlDatabase,
        cfg: OnboardingConfig,
        company_id: str,
    ) -> None:
        self._db = db
        self._cfg = cfg
        self._company = company_id

    # ------------------------------------------------------------------ setup
    def seed_templates(self) -> int:
        with self._db.unit_of_work() as uow:
            return OnboardingRepository(uow).upsert_templates(self._company, self._cfg.templates)

    def ensure_plans(self, asof: date) -> list[str]:
        """Create plans for everyone hired inside the window who has none yet. Idempotent."""
        since = asof - timedelta(days=self._cfg.new_hire_window_days)
        with self._db.unit_of_work() as uow:
            hires = OnboardingRepository(uow).recent_hires(asof, since)
        return [pid for pid in hires if self.ensure_plan(pid, asof)]

    def ensure_plan(self, person_id: str, asof: date) -> bool:
        with self._db.unit_of_work() as uow:
            repo = OnboardingRepository(uow)
            if repo.plan(person_id) is not None:
                return False
            d = DirectoryReader(uow)
            person = d.person(person_id)
            if person is None or person.hired_on is None:
                raise NotFoundError(f"no hire record for {person_id}")
            if not repo.templates():
                raise NwbError("no onboarding templates; run `nwb onboarding seed`")
            buddy = self._pick_buddy(uow, person_id, asof)
            repo.create_plan(new_id("ob"), person_id, person.hired_on, buddy)
        dataflow(Hop.JOB, job="onboarding_plan", person=person_id, buddy=buddy)
        return True

    def _pick_buddy(self, uow: UnitOfWork, person_id: str, asof: date) -> str | None:
        """Tenured, same or slightly higher grade, not the manager. Same unit only when configured."""
        d = DirectoryReader(uow)
        me = d.person(person_id)
        if me is None:
            return None
        units = [m.unit_id for m in d.memberships(person_id, asof)]
        if self._cfg.buddy.same_unit and not units:
            return None
        mgr = d.manager(person_id, asof)
        exclude = [person_id, *([mgr.id] if mgr else [])]
        cutoff = asof - timedelta(days=self._cfg.buddy.min_tenure_days)
        unit = units[0] if self._cfg.buddy.same_unit else None
        rows = OnboardingRepository(uow).buddy_candidates(exclude, cutoff, unit)
        gap = self._cfg.buddy.max_grade_gap
        mine = _grade(me.grade)
        fits = [r for r in rows if 0 <= _grade(r["grade"]) - mine <= gap]
        fits.sort(key=lambda r: (_grade(r["grade"]) - mine, str(r["id"])))
        return str(fits[0]["id"]) if fits else None

    # ------------------------------------------------------------------ read
    def plan(self, person_id: str) -> PlanRow | None:
        with self._db.unit_of_work(as_person=person_id) as uow:
            return OnboardingRepository(uow).plan(person_id)

    @staticmethod
    def day(plan: PlanRow, asof: date) -> int:
        return (asof - plan.start_date).days + 1

    def _items(self, person_id: str) -> list[ItemRow]:
        with self._db.unit_of_work(as_person=person_id) as uow:
            return OnboardingRepository(uow).items(person_id)

    def open_items(self, person_id: str, asof: date) -> tuple[list[BriefItem], list[BriefItem]]:
        """(overdue, upcoming within the lookahead), both open only."""
        plan = self.plan(person_id)
        if plan is None:
            return [], []
        today = self.day(plan, asof)
        horizon = today + self._cfg.brief.lookahead_days
        overdue: list[BriefItem] = []
        upcoming: list[BriefItem] = []
        for it in self._items(person_id):
            if it.status in {"done", "waived"}:
                continue
            bi = BriefItem(
                id=it.id,
                title=it.title,
                category=it.category,
                due_day=it.due_day,
                due_date=plan.start_date + timedelta(days=it.due_day - 1),
                status=it.status,
                overdue=it.due_day < today,
                mandatory=it.mandatory,
                ref=it.target_ref,
            )
            if bi.overdue:
                overdue.append(bi)
            elif it.due_day <= horizon:
                upcoming.append(bi)
        return overdue, upcoming

    def brief(self, person_id: str, asof: date) -> OnboardingBrief:
        plan = self.plan(person_id)
        if plan is None:
            raise NotFoundError("no onboarding plan for this user")
        day = self.day(plan, asof)
        with self._db.unit_of_work() as uow:
            d = DirectoryReader(uow)
            me = d.person(person_id)
            mgr = d.manager(person_id, asof)
            buddy = d.person(plan.buddy_id) if plan.buddy_id else None
        items = self._items(person_id)
        mand = [i for i in items if i.mandatory]
        overdue, upcoming = self.open_items(person_id, asof)
        n = self._cfg.brief.max_items
        out = OnboardingBrief(
            person_id=person_id,
            name=me.name if me else person_id,
            day=day,
            phase=self._cfg.phase_for(day),
            start_date=plan.start_date,
            manager=f"{mgr.name} ({mgr.id})" if mgr else None,
            buddy=f"{buddy.name} ({buddy.id})" if buddy else None,
            progress=f"{sum(i.status == 'done' for i in mand)}/{len(mand)}",
            overdue=overdue[:n],
            upcoming=upcoming[: max(0, n - len(overdue[:n]))],
        )
        dataflow(Hop.RETRIEVAL, kind="onboarding_brief", person=person_id, day=day, overdue=len(overdue))
        return out

    # ------------------------------------------------------------------ write
    def update_item(self, person_id: str, item_id: str, status: ItemStatus, evidence: str | None) -> ItemUpdate:
        with self._db.unit_of_work(as_person=person_id) as uow:
            ok = OnboardingRepository(uow).set_item_status(person_id, item_id, status, evidence)
        if not ok:
            raise NotFoundError(f"no onboarding item {item_id} for this user")
        dataflow(Hop.TOOL_CALL, kind="onboarding_item", person=person_id, item=item_id, status=status)
        return ItemUpdate(item_id=item_id, status=status, ok=True)

    # --------------------------------------------------------------- beat machine
    def machine_beats(self) -> tuple[Beat, ...]:
        return tuple(_beat(spec) for spec in self._cfg.beats)

    def hire_facts(self, person_id: str, asof: date) -> dict[str, object]:
        """Facts a beat predicate may name. Computed from the directory, not from a persona list."""
        plan = self.plan(person_id)
        with self._db.unit_of_work() as uow:
            manager = DirectoryReader(uow).manager(person_id, asof)
        overdue, upcoming = self.open_items(person_id, asof) if plan else ([], [])
        day = self.day(plan, asof) if plan else 0
        return {
            "has_buddy": bool(plan and plan.buddy_id),
            "has_manager": manager is not None,
            "open_items": len(overdue) + len(upcoming),
            "day": day,
            "phase": self._cfg.phase_for(day) if plan else "",
        }

    def load_flow(self, person_id: str) -> FlowRecord | None:
        with self._db.unit_of_work(as_person=person_id) as uow:
            raw = OnboardingRepository(uow).beat_record(person_id)
        if not raw:
            return None
        return FlowRecord.model_validate_json(raw)

    def save_flow(self, person_id: str, record: FlowRecord) -> None:
        with self._db.unit_of_work(as_person=person_id) as uow:
            repo = OnboardingRepository(uow)
            plan = repo.plan(person_id)
            if plan is None:
                raise NotFoundError("no onboarding plan for this user")
            repo.write_beat_record(person_id, record.model_dump_json())
            if record.phase != ONBOARDING:
                repo.mark_welcomed(plan.id)

    def prepare_flow(self, person_id: str, asof: date) -> tuple[FlowRecord, tuple[Beat, ...], dict[str, object]]:
        beats = self.machine_beats()
        facts = self.hire_facts(person_id, asof)
        stored = self.load_flow(person_id)
        record = align(stored, beats, facts) if stored is not None else new_record(beats, facts, now=asof.isoformat())
        return record, beats, facts

    def allows(self, purpose: str) -> bool:
        return purpose in self._cfg.onboard_purposes

    def apply_event(self, person_id: str, asof: date, event: Event, purpose: str) -> OnboardingStep:
        """Move the machine. Refuses purposes that are not allowed to onboard, and does not write then."""
        if purpose not in self._cfg.onboard_purposes:
            dataflow(Hop.JOB, job="onboarding_beat", person=person_id, purpose=purpose, refused=True)
            return OnboardingStep(phase=ONBOARDING, error="onboarding does not run for this purpose")
        plan = self.plan(person_id)
        if plan is None:
            raise NotFoundError("no onboarding plan for this user")
        record, beats, facts = self.prepare_flow(person_id, asof)
        if plan.welcomed or record.phase != ONBOARDING:
            return OnboardingStep(phase=record.phase, completed=True, error="onboarding is already finished")
        step = apply(record, event, beats, facts, now=asof.isoformat())
        if step.result.error is None:
            self.save_flow(person_id, step.record)
        dataflow(
            Hop.JOB,
            job="onboarding_beat",
            person=person_id,
            beat=step.result.beat,
            phase=step.record.phase,
            purpose=purpose,
        )
        return OnboardingStep(
            beat=step.result.beat,
            phase=step.record.phase,
            next_beat=step.result.next_beat,
            skipped=step.result.skipped,
            completed=step.record.phase != ONBOARDING,
            error=step.result.error,
        )


def _beat(spec: OnboardingBeat) -> Beat:
    return Beat(
        spec.id,
        When(
            all_of=tuple(Atom(atom.fact, atom.op, atom.value) for atom in spec.when.every),
            any_of=tuple(Atom(atom.fact, atom.op, atom.value) for atom in spec.when.some),
        ),
    )
