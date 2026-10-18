"""Layer 1 loader: org units, people, dated memberships/reporting lines, groups, ACL tag rules -> SQL + outbox.

Idempotent: re-running replaces loader-owned history and upserts entities in one transaction.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date

from nwb.conf.models import DirectoryRoles
from nwb.db.models import (
    AccessRule,
    Company,
    Group,
    GroupMember,
    Membership,
    OrgUnit,
    Person,
    SubjectType,
)
from nwb.db.repo_org import OrgRepository
from nwb.ingest.acl import classify_tag
from nwb.ingest.sources import DirectorySource, HrisSource, ScimUser
from nwb.ingest.timeline import build_memberships, build_reporting_lines
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.obs.log import asdict_safe as _asdict
from nwb.ports import SqlDatabase

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Layer1Report:
    units: int
    people: int
    memberships: int
    reporting_lines: int
    groups: int
    group_members: int
    access_rules: int
    skipped_lines: int


def unit_names(users: list[ScimUser]) -> dict[str, str]:
    """Unit id -> display name, from the directory (SCIM enterprise ``department`` code -> ``division`` name)."""
    seen: dict[str, Counter[str]] = {}
    for u in users:
        if u.department and u.division:
            seen.setdefault(u.department, Counter())[u.division] += 1
    return {code: c.most_common(1)[0][0] for code, c in seen.items()}


class Layer1Loader:
    def __init__(
        self, db: SqlDatabase, directory: DirectorySource, hris: HrisSource, company: Company, roles: DirectoryRoles
    ) -> None:
        self._db = db
        self._dir = directory
        self._hris = hris
        self._company = company
        self._roles = roles

    @traced(step="layer1.load", level=logging.INFO)
    def load(self) -> Layer1Report:
        users = self._dir.users()
        groups = self._dir.groups()
        workers = {w.id: w for w in self._hris.workers()}
        events = self._hris.job_events()
        dataflow(Hop.INGEST, loader="layer1", users=len(users), groups=len(groups), workers=len(workers))
        cid = self._company.id
        known = {u.id for u in users} | set(workers)

        terminated = {e.worker_id: e.effective for e in events if e.event_type == "Termination"}
        people = [
            Person(
                id=u.id,
                company_id=cid,
                name=u.name,
                email=u.email,
                title=(workers[u.id].title if u.id in workers else u.title),
                grade=(workers[u.id].grade if u.id in workers else u.grade),
                location=(workers[u.id].location if u.id in workers else u.location),
                country=u.country,
                status="left" if u.id in terminated else ("active" if u.active else "suspended"),
                external=u.external,
                hired_on=u.hire_date,
                left_on=terminated.get(u.id),
                src_id=f"scim:{u.id}",
            )
            for u in users
        ]

        members_of = {g.id: set(g.member_ids) for g in groups}
        root = self._roles.root_unit
        leaders = {p: True for gid in self._roles.leader_groups for p in members_of.get(gid, set())}
        memberships = build_memberships(events, leaders)
        hire = {p.id: p.hired_on or date.min for p in people if not p.external}
        exec_members = set().union(*(members_of.get(g, set()) for g in self._roles.executive_groups))
        execs = sorted(exec_members & set(hire))
        memberships += [
            Membership(
                person_id=p,
                unit_id=root,
                valid_from=hire[p],
                is_leader=True,
                is_primary=False,
                role="executive",
                src_id="scim:executive_groups",
            )
            for p in execs
        ]
        unit_ids = sorted({m.unit_id for m in memberships} | {w.unit for w in workers.values()})
        names = unit_names(users)
        units = [OrgUnit(id=root, company_id=cid, kind="department", name=self._company.name)] + [
            OrgUnit(
                id=u,
                company_id=cid,
                kind="department",
                name=names.get(u, u),
                parent_id=root,
                dept_id=u,
                src_id=f"hris:unit:{u}",
            )
            for u in unit_ids
            if u != root
        ]

        lines = build_reporting_lines(events, {w: workers[w].manager_id for w in workers}, hire)
        valid_lines = [ln for ln in lines if ln.manager_id in known and ln.person_id in known]
        skipped = len(lines) - len(valid_lines)

        db_groups = [Group(id=g.id, company_id=cid, name=g.name, src_id=f"scim:{g.id}") for g in groups]
        gm: list[GroupMember] = []
        for g in groups:
            for pid in g.member_ids:
                if pid not in known:
                    skipped += 1
                    continue
                start = g.window_from or max(g.created, hire.get(pid, g.created))
                end = g.window_to or terminated.get(pid)
                if end is not None and end < start:
                    continue
                gm.append(GroupMember(group_id=g.id, person_id=pid, valid_from=start, valid_to=end))

        rules: list[AccessRule] = []
        for tag, subjects in self._dir.acl_classifications().items():
            cls, share = classify_tag(tag)
            subj: tuple[tuple[SubjectType, str], ...] = tuple(
                ("group" if s.startswith("grp-") else "person", s) for s in subjects
            )
            rules.append(
                AccessRule(
                    id=f"{cid}:{tag}",
                    company_id=cid,
                    source_tag=tag,
                    classification=cls,
                    share_scope=share,
                    subjects=subj,
                )
            )

        pids = [p.id for p in people]
        with self._db.unit_of_work() as uow:
            repo = OrgRepository(uow)
            repo.upsert_company(self._company)
            n_units = repo.upsert_units(units)
            repo.rebuild_closure(cid)
            n_people = repo.upsert_people(people)
            n_mem = repo.replace_memberships(pids, memberships)
            n_lines = repo.replace_reporting_lines(pids, valid_lines)
            n_groups = repo.upsert_groups(db_groups)
            n_gm = repo.replace_group_members([g.id for g in db_groups], gm)
            n_rules = repo.upsert_access_rules(rules)
        report = Layer1Report(n_units, n_people, n_mem, n_lines, n_groups, n_gm, n_rules, skipped)
        log.info("layer1 loaded: %s", report)
        dataflow(Hop.SQL_WRITE, loader="layer1", **_asdict(report))
        return report
