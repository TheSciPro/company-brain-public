"""OrgRepository: single writer for org/people/groups/access tables. Every write also appends its outbox row."""

from __future__ import annotations

from collections.abc import Iterable

from nwb.db.models import (
    AccessRule,
    Company,
    Group,
    GroupMember,
    Membership,
    OrgUnit,
    Person,
    ReportingLine,
)
from nwb.db.outbox import OutboxRepository
from nwb.ports import UnitOfWork


class OrgRepository:
    def __init__(self, uow: UnitOfWork, outbox: OutboxRepository | None = None) -> None:
        self._uow = uow
        self._outbox = outbox or OutboxRepository(uow)

    # ---- companies / units -------------------------------------------------
    def upsert_company(self, c: Company) -> None:
        self._uow.execute(
            "INSERT INTO companies (id, name) VALUES (:id, :name) ON CONFLICT (id) DO UPDATE SET name = excluded.name",
            c.model_dump(),
        )
        self._outbox.add("company", c.id, c.model_dump(mode="json"))

    def upsert_units(self, units: Iterable[OrgUnit]) -> int:
        units = list(units)
        rows = [u.model_dump() for u in units]
        self._uow.executemany(
            "INSERT INTO org_units (id, company_id, kind, name, parent_id, dept_id, valid_from, valid_to, src_id) "
            "VALUES (:id, :company_id, :kind, :name, :parent_id, :dept_id, :valid_from, :valid_to, :src_id) "
            "ON CONFLICT (id) DO UPDATE SET kind = excluded.kind, name = excluded.name, "
            "parent_id = excluded.parent_id, dept_id = excluded.dept_id, valid_from = excluded.valid_from, "
            "valid_to = excluded.valid_to, src_id = excluded.src_id",
            rows,
        )
        self._outbox.add_many("unit", [(u.id, u.model_dump(mode="json")) for u in units])
        return len(rows)

    def rebuild_closure(self, company_id: str) -> int:
        """Recompute org_unit_closure (ancestor, descendant, depth) for one company."""
        units = self._uow.fetchall("SELECT id, parent_id FROM org_units WHERE company_id = :c", {"c": company_id})
        parent = {str(u["id"]): u["parent_id"] for u in units}
        pairs: list[dict[str, object]] = []
        for uid in parent:
            node: str | None = uid
            depth = 0
            seen: set[str] = set()
            while node is not None:
                if node in seen:
                    raise ValueError(f"cycle in org_units at {node}")
                seen.add(node)
                pairs.append({"a": node, "d": uid, "n": depth})
                node = parent.get(node)
                depth += 1
        self._uow.execute(
            "DELETE FROM org_unit_closure WHERE descendant_id IN (SELECT id FROM org_units WHERE company_id = :c)",
            {"c": company_id},
        )
        self._uow.executemany(
            "INSERT INTO org_unit_closure (ancestor_id, descendant_id, depth) VALUES (:a, :d, :n)", pairs
        )
        return len(pairs)

    # ---- people --------------------------------------------------------------
    def upsert_people(self, people: Iterable[Person]) -> int:
        items = list(people)
        self._uow.executemany(
            "INSERT INTO people (id, company_id, name, email, title, grade, location, country, status, "
            "external, hired_on, left_on, src_id) VALUES (:id, :company_id, :name, :email, :title, :grade, :location, "
            ":country, :status, :external, :hired_on, :left_on, :src_id) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, email = excluded.email, title = excluded.title, "
            "grade = excluded.grade, location = excluded.location, country = excluded.country, "
            "status = excluded.status, "
            "external = excluded.external, hired_on = excluded.hired_on, left_on = excluded.left_on, "
            "src_id = excluded.src_id",
            [p.model_dump() for p in items],
        )
        self._outbox.add_many("person", [(p.id, p.model_dump(mode="json", exclude={"email"})) for p in items])
        return len(items)

    def replace_memberships(self, person_ids: list[str], memberships: Iterable[Membership]) -> int:
        """Loader-owned history: replace the dated memberships of the given people (idempotent reload)."""
        items = list(memberships)
        self._uow.executemany("DELETE FROM memberships WHERE person_id = :p", [{"p": p} for p in person_ids])
        self._uow.executemany(
            "INSERT INTO memberships (id, person_id, unit_id, role, is_leader, is_primary, valid_from, valid_to, "
            "src_id) VALUES (:id, :person_id, :unit_id, :role, :is_leader, :is_primary, :valid_from, :valid_to, "
            ":src_id)",
            [{**m.model_dump(), "id": m.id} for m in items],
        )
        self._outbox.add_many("membership", [(m.id, {**m.model_dump(mode="json"), "id": m.id}) for m in items])
        return len(items)

    def replace_reporting_lines(self, person_ids: list[str], lines: Iterable[ReportingLine]) -> int:
        items = list(lines)
        self._uow.executemany("DELETE FROM reporting_lines WHERE person_id = :p", [{"p": p} for p in person_ids])
        self._uow.executemany(
            "INSERT INTO reporting_lines (person_id, manager_id, valid_from, valid_to, src_id) "
            "VALUES (:person_id, :manager_id, :valid_from, :valid_to, :src_id)",
            [r.model_dump() for r in items],
        )
        self._outbox.add_many(
            "reporting_line", [(f"{r.person_id}:{r.valid_from}", r.model_dump(mode="json")) for r in items]
        )
        return len(items)

    # ---- groups / access -----------------------------------------------------
    def upsert_groups(self, groups: Iterable[Group]) -> int:
        items = list(groups)
        self._uow.executemany(
            "INSERT INTO groups (id, company_id, name, src_id) VALUES (:id, :company_id, :name, :src_id) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, src_id = excluded.src_id",
            [g.model_dump() for g in items],
        )
        self._outbox.add_many("group", [(g.id, g.model_dump(mode="json")) for g in items])
        return len(items)

    def replace_group_members(self, group_ids: list[str], members: Iterable[GroupMember]) -> int:
        items = list(members)
        self._uow.executemany("DELETE FROM group_members WHERE group_id = :g", [{"g": g} for g in group_ids])
        self._uow.executemany(
            "INSERT INTO group_members (group_id, person_id, valid_from, valid_to) "
            "VALUES (:group_id, :person_id, :valid_from, :valid_to)",
            [m.model_dump() for m in items],
        )
        self._outbox.add_many(
            "group_member",
            [(f"{m.group_id}:{m.person_id}:{m.valid_from}", m.model_dump(mode="json")) for m in items],
        )
        return len(items)

    def upsert_access_rules(self, rules: Iterable[AccessRule]) -> int:
        items = list(rules)
        for r in items:
            self._uow.execute(
                "INSERT INTO access_rules (id, company_id, source_tag, classification, share_scope) "
                "VALUES (:id, :company_id, :source_tag, :classification, :share_scope) "
                "ON CONFLICT (id) DO UPDATE SET classification = excluded.classification, "
                "share_scope = excluded.share_scope",
                r.model_dump(exclude={"subjects"}),
            )
            self._uow.execute("DELETE FROM access_rule_subjects WHERE rule_id = :id", {"id": r.id})
            self._uow.executemany(
                "INSERT INTO access_rule_subjects (rule_id, subject_type, subject_id) VALUES (:r, :t, :s)",
                [{"r": r.id, "t": t, "s": s} for t, s in r.subjects],
            )
            self._outbox.add("access_rule", r.id, r.model_dump(mode="json"))
        return len(items)

    # ---- reads (system scope; user-facing reads go through AccessPolicy) -----
    def count(self, table: str) -> int:
        if table not in _COUNTABLE:
            raise ValueError(table)
        row = self._uow.fetchone(f"SELECT count(*) AS n FROM {table}")
        return int(row["n"]) if row else 0


_COUNTABLE = frozenset(
    {
        "companies",
        "org_units",
        "org_unit_closure",
        "people",
        "memberships",
        "reporting_lines",
        "groups",
        "group_members",
        "access_rules",
        "access_rule_subjects",
        "graph_outbox",
    }
)
