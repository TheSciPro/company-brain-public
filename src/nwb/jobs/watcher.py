"""Watcher: periodic rules that *propose* actions into the approval queue. It never executes anything itself.

Rules live in ``config/actions.yaml#watcher``; each maps a condition to an action kind whose approver comes from
the kind's config. Proposals are idempotent through a dedupe key (rule + subject + period).

Each run re-reads the YAML it is configured to watch. A file whose bytes changed is reloaded and used for
this run; a file that did not change stays cached. Both lists are logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from nwb.actions.service import ActionService
from nwb.conf.loader import ConfigLoader, ReloadReport
from nwb.conf.models import ActionsConfig, WatchRule
from nwb.db.repo_directory import DirectoryReader
from nwb.errors import NotFoundError
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.ports import SqlDatabase

log = get_logger(__name__)


@dataclass
class WatcherReport:
    asof: date
    proposed: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    reloaded: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def summary(self) -> str:
        rules = f"watcher {self.asof}: proposed {len(self.proposed)}, already queued {len(self.already)}, " + (
            f"skipped {len(self.skipped)}: {'; '.join(self.skipped)}" if self.skipped else "skipped 0"
        )
        return f"{rules}; reloaded [{', '.join(self.reloaded) or '-'}]; unchanged [{', '.join(self.unchanged) or '-'}]"


def _d(v: object) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


class WatcherJob:
    def __init__(
        self,
        db: SqlDatabase,
        actions: ActionService,
        cfg: ActionsConfig,
        *,
        loader: ConfigLoader | None = None,
    ) -> None:
        self._db = db
        self._actions = actions
        self._cfg = cfg
        self._loader = loader

    def _refresh(self) -> ReloadReport:
        """Reload changed YAML. ``actions`` is always included: it names the rest of the list."""
        if self._loader is None:
            return ReloadReport((), ())
        names = ("actions", *self._cfg.watcher.reload)
        report = self._loader.refresh(names)
        # Always re-read actions through the loader: a cache hit when nothing changed, a parse when it did.
        # An invalid file fails this run instead of leaving the previous rules in place.
        self._cfg = self._loader.load("actions", ActionsConfig)
        self._actions.bind_config(self._cfg)
        extra = tuple(name for name in self._cfg.watcher.reload if name not in names)
        if extra:
            more = self._loader.refresh(extra)
            report = ReloadReport(report.reloaded + more.reloaded, report.unchanged + more.unchanged)
        reloaded, unchanged = list(report.reloaded), list(report.unchanged)
        log.info(
            "watcher config refresh reloaded=%s unchanged=%s",
            ",".join(reloaded) or "-",
            ",".join(unchanged) or "-",
            reloaded=reloaded,
            unchanged=unchanged,
        )
        dataflow(Hop.JOB, kind="watcher_reload", reloaded=reloaded, unchanged=unchanged)
        return report

    @traced(step="watcher.run", level=logging.INFO)
    def run(self, asof: date) -> WatcherReport:
        refreshed = self._refresh()
        report = WatcherReport(asof, reloaded=list(refreshed.reloaded), unchanged=list(refreshed.unchanged))
        if not self._cfg.watcher.enabled:
            report.skipped.append("watcher disabled")
            return report
        rules = self._cfg.watcher.rules
        if (r := rules.get("overdue_onboarding")) and r.enabled:
            self._overdue_onboarding(r, asof, report)
        if (r := rules.get("expiring_grants")) and r.enabled:
            self._expiring_grants(r, asof, report)
        dataflow(
            Hop.JOB, kind="watcher", asof=asof.isoformat(), proposed=len(report.proposed), already=len(report.already)
        )
        return report

    def _record(self, report: WatcherReport, view_note: str | None, action_id: str) -> None:
        (report.already if view_note == "already proposed" else report.proposed).append(action_id)

    def _overdue_onboarding(self, rule: WatchRule, asof: date, report: WatcherReport) -> None:
        with self._db.unit_of_work() as uow:
            rows = uow.fetchall(
                "SELECT p.person_id, p.start_date, i.id, i.title, i.due_day FROM onboarding_plans p "
                "JOIN onboarding_items i ON i.plan_id = p.id "
                "WHERE p.phase NOT IN ('graduated', 'paused') AND i.status IN ('todo', 'doing', 'blocked') "
                "AND i.mandatory ORDER BY p.person_id, i.due_day"
            )
            names = {}
            late: dict[str, list[str]] = {}
            for row in rows:
                due = _d(row["start_date"]) + timedelta(days=int(row["due_day"]))
                if asof - due >= timedelta(days=rule.min_days_overdue):
                    pid = str(row["person_id"])
                    late.setdefault(pid, []).append(str(row["title"]))
            d = DirectoryReader(uow)
            for pid in late:
                p = d.person(pid)
                names[pid] = p.name if p else pid
        week = asof.isocalendar()
        for pid, titles in late.items():
            msg = (
                f"{names[pid]} has {len(titles)} onboarding item(s) overdue by {rule.min_days_overdue}+ days: "
                + "; ".join(titles[:5])
            )
            try:
                view = self._actions.propose(
                    kind_name=rule.action,
                    requester=pid,
                    target=pid,
                    title=f"Overdue onboarding: {names[pid]}",
                    args={"message": msg, "items": titles},
                    asof=asof,
                    proposed_by="watcher:overdue_onboarding",
                    dedupe_key=f"watch:overdue:{pid}:{week.year}-W{week.week:02d}",
                )
            except NotFoundError as exc:
                report.skipped.append(f"{pid}: {exc}")
                continue
            self._record(report, view.note, view.action_id)

    def _expiring_grants(self, rule: WatchRule, asof: date, report: WatcherReport) -> None:
        until = asof + timedelta(days=rule.within_days)
        with self._db.unit_of_work() as uow:
            rows = uow.fetchall(
                "SELECT g.id, g.subject_type, g.subject_id, g.resource_id, g.valid_to, d.owner_person_id "
                "FROM grants g JOIN documents d ON d.id = g.resource_id "
                "WHERE g.valid_to IS NOT NULL AND g.valid_to >= :a AND g.valid_to <= :u "
                "AND d.owner_person_id IS NOT NULL",
                {"a": asof, "u": until},
            )
        for row in rows:
            owner = str(row["owner_person_id"])
            subject = f"{row['subject_type']}:{row['subject_id']}"
            msg = f"Access for {subject} to {row['resource_id']} ends on {_d(row['valid_to'])}. Renew or let it lapse?"
            view = self._actions.propose(
                kind_name=rule.action,
                requester=owner,
                target=owner,
                title=f"Access expiring: {row['resource_id']}",
                args={
                    "message": msg,
                    "resource_id": str(row["resource_id"]),
                    "grant_id": str(row["id"]),
                    "subject": subject,
                },
                asof=asof,
                proposed_by="watcher:expiring_grants",
                dedupe_key=f"watch:grant:{row['id']}:{_d(row['valid_to'])}",
            )
            self._record(report, view.note, view.action_id)
