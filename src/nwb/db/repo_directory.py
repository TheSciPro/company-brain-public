"""DirectoryReader: read-only, dated views over people/org tables (the "heart" of the account block)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nwb.ports import UnitOfWork

_ACTIVE = "valid_from <= :asof AND (valid_to IS NULL OR valid_to >= :asof)"


@dataclass(frozen=True, slots=True)
class PersonView:
    id: str
    name: str
    title: str | None
    grade: str | None
    location: str | None
    status: str
    hired_on: date | None
    external: bool
    country: str | None = None


@dataclass(frozen=True, slots=True)
class UnitMembership:
    unit_id: str
    unit_name: str
    kind: str
    role: str | None
    is_leader: bool


@dataclass(frozen=True, slots=True)
class AccessSummary:
    groups: tuple[str, ...]
    grant_count: int
    next_grant_expiry: date | None
    leads_units: tuple[str, ...]


class DirectoryReader:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def person(self, person_id: str) -> PersonView | None:
        r = self._uow.fetchone(
            "SELECT id, name, title, grade, location, country, status, hired_on, external FROM people WHERE id = :id",
            {"id": person_id},
        )
        if r is None:
            return None
        hired = r["hired_on"]
        return PersonView(
            id=str(r["id"]),
            name=str(r["name"]),
            title=r["title"],
            grade=r["grade"],
            location=r["location"],
            country=r["country"],
            status=str(r["status"]),
            external=bool(r["external"]),
            hired_on=hired if isinstance(hired, date) or hired is None else date.fromisoformat(str(hired)[:10]),
        )

    def memberships(self, person_id: str, asof: date) -> list[UnitMembership]:
        rows = self._uow.fetchall(
            "SELECT m.unit_id, u.name, u.kind, m.role, m.is_leader "
            "FROM memberships m JOIN org_units u ON u.id = m.unit_id "
            "WHERE m.person_id = :p AND m.valid_from <= :asof AND (m.valid_to IS NULL OR m.valid_to >= :asof) "
            "ORDER BY m.is_primary DESC, u.kind",
            {"p": person_id, "asof": asof},
        )
        return [
            UnitMembership(str(r["unit_id"]), str(r["name"]), str(r["kind"]), r["role"], bool(r["is_leader"]))
            for r in rows
        ]

    def manager(self, person_id: str, asof: date) -> PersonView | None:
        r = self._uow.fetchone(
            f"SELECT manager_id FROM reporting_lines WHERE person_id = :p AND {_ACTIVE} ORDER BY valid_from DESC",
            {"p": person_id, "asof": asof},
        )
        return self.person(str(r["manager_id"])) if r else None

    def find_people(self, name: str, limit: int = 5) -> list[PersonView]:
        rows = self._uow.fetchall(
            "SELECT id FROM people WHERE lower(name) LIKE :n OR id = :raw ORDER BY status, id LIMIT :k",
            {"n": f"%{name.lower().strip()}%", "raw": name.strip(), "k": limit},
        )
        out = [self.person(str(r["id"])) for r in rows]
        return [p for p in out if p is not None]

    def access_summary(self, person_id: str, asof: date) -> AccessSummary:
        groups = self._uow.fetchall(
            f"SELECT group_id FROM group_members WHERE person_id = :p AND {_ACTIVE} ORDER BY group_id",
            {"p": person_id, "asof": asof},
        )
        grants = self._uow.fetchone(
            "SELECT COUNT(*) AS n, MIN(valid_to) AS nxt FROM grants WHERE subject_type = 'person' AND subject_id = :p "
            f"AND {_ACTIVE}",
            {"p": person_id, "asof": asof},
        )
        leads = [m.unit_name for m in self.memberships(person_id, asof) if m.is_leader]
        nxt = grants["nxt"] if grants else None
        return AccessSummary(
            groups=tuple(str(g["group_id"]) for g in groups),
            grant_count=int(grants["n"]) if grants else 0,
            next_grant_expiry=nxt if isinstance(nxt, date) or nxt is None else date.fromisoformat(str(nxt)[:10]),
            leads_units=tuple(leads),
        )

    def entity_names(self) -> list[str]:
        """Org unit names and active people, for Graphiti to reuse instead of minting a second node."""
        rows = self._uow.fetchall(
            "SELECT name FROM org_units UNION SELECT name FROM people WHERE status = 'active' ORDER BY name"
        )
        return [str(r["name"]) for r in rows if r["name"]]

    def group_ids(self, person_id: str, asof: date) -> list[str]:
        return list(self.access_summary(person_id, asof).groups)

    def reporting_chain(self, person_id: str, asof: date, depth: int) -> list[PersonView]:
        """Managers above ``person_id`` as of ``asof``, nearest first (cycle-safe)."""
        chain: list[PersonView] = []
        seen = {person_id}
        current = person_id
        for _ in range(depth):
            mgr = self.manager(current, asof)
            if mgr is None or mgr.id in seen:
                break
            chain.append(mgr)
            seen.add(mgr.id)
            current = mgr.id
        return chain
