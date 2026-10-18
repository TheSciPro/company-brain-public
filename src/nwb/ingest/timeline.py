"""Pure functions: job events -> dated memberships and reporting lines (bi-temporal 'valid' axis)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from nwb.db.models import Membership, ReportingLine
from nwb.ingest.sources import JobEvent

_ONE_DAY = timedelta(days=1)


def _by_worker(events: Iterable[JobEvent]) -> dict[str, list[JobEvent]]:
    grouped: dict[str, list[JobEvent]] = defaultdict(list)
    for e in events:
        grouped[e.worker_id].append(e)
    for evs in grouped.values():
        evs.sort(key=lambda e: (e.effective, e.id))
    return grouped


def build_memberships(events: Iterable[JobEvent], leaders: dict[str, bool] | None = None) -> list[Membership]:
    """Hire opens a unit membership; a unit change closes it (day before) and opens the next; Termination closes.

    ``leaders`` marks people whose unit memberships carry leadership (members of the configured leader groups).
    """
    leaders = leaders or {}
    out: list[Membership] = []
    for wid, evs in _by_worker(events).items():
        unit: str | None = None
        start: date | None = None
        src: str | None = None
        for e in evs:
            new_unit = e.after.get("unit")
            terminated = e.event_type == "Termination"
            if unit is not None and start is not None and (terminated or (new_unit and new_unit != unit)):
                end = max(start, e.effective - _ONE_DAY) if not terminated else e.effective
                out.append(_m(wid, unit, start, end, src, leaders))
                unit = None
            if terminated:
                break
            if new_unit and new_unit != unit:
                unit, start, src = str(new_unit), e.effective, f"hris:{e.id}"
        if unit is not None and start is not None:
            out.append(_m(wid, unit, start, None, src, leaders))
    return out


def _m(wid: str, unit: str, start: date, end: date | None, src: str | None, leaders: dict[str, bool]) -> Membership:
    return Membership(
        person_id=wid,
        unit_id=unit,
        valid_from=start,
        valid_to=end,
        is_leader=leaders.get(wid, False),
        src_id=src,
    )


def build_reporting_lines(
    events: Iterable[JobEvent], current_manager: dict[str, str | None], hire_dates: dict[str, date]
) -> list[ReportingLine]:
    """Walk manager changes backwards from the current manager (workers.csv) to the hire date."""
    grouped = _by_worker(events)
    out: list[ReportingLine] = []
    for wid, hired in hire_dates.items():
        changes = [e for e in grouped.get(wid, []) if "manager" in e.after or "manager" in e.before]
        terminated = next((e.effective for e in grouped.get(wid, []) if e.event_type == "Termination"), None)
        end: date | None = terminated
        manager = current_manager.get(wid)
        for e in reversed(changes):
            if manager:
                out.append(
                    ReportingLine(
                        person_id=wid,
                        manager_id=manager,
                        valid_from=e.effective,
                        valid_to=end,
                        src_id=f"hris:{e.id}",
                    )
                )
            end = e.effective - _ONE_DAY
            manager = e.before.get("manager")
        if manager and (end is None or end >= hired):
            out.append(
                ReportingLine(person_id=wid, manager_id=manager, valid_from=hired, valid_to=end, src_id="hris:workers")
            )
    return out
