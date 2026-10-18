"""Onboarding plans/items/templates and the in-app inbox (SQL only; rules live in nwb.onboarding)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from nwb.conf.models import OnboardingTemplate
from nwb.ports import Row, UnitOfWork


def _date(v: object) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


@dataclass(frozen=True, slots=True)
class PlanRow:
    id: str
    person_id: str
    start_date: date
    phase: str
    buddy_id: str | None
    welcomed: bool


@dataclass(frozen=True, slots=True)
class ItemRow:
    id: str
    category: str
    title: str
    target_ref: str | None
    due_day: int
    mandatory: bool
    status: str


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: str
    kind: str
    title: str
    body: str
    ref: str | None
    created_at: str
    read: bool


class OnboardingRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def upsert_templates(self, company_id: str, templates: tuple[OnboardingTemplate, ...]) -> int:
        for t in templates:
            self._uow.execute("DELETE FROM onboarding_templates WHERE id = :id", {"id": t.id})
            self._uow.execute(
                "INSERT INTO onboarding_templates (id, company_id, category, title, target_ref, due_day, mandatory, "
                "verify_rule) VALUES (:id, :c, :cat, :t, :ref, :d, :m, :v)",
                {
                    "id": t.id,
                    "c": company_id,
                    "cat": t.category,
                    "t": t.title,
                    "ref": t.target_ref,
                    "d": t.due_day,
                    "m": t.mandatory,
                    "v": t.verify_rule,
                },
            )
        return len(templates)

    def templates(self) -> list[Row]:
        return self._uow.fetchall("SELECT * FROM onboarding_templates ORDER BY due_day, id")

    def plan(self, person_id: str) -> PlanRow | None:
        r = self._uow.fetchone(
            "SELECT id, person_id, start_date, phase, buddy_id, welcomed_at FROM onboarding_plans WHERE person_id = :p",
            {"p": person_id},
        )
        if r is None:
            return None
        return PlanRow(
            id=str(r["id"]),
            person_id=str(r["person_id"]),
            start_date=_date(r["start_date"]),
            phase=str(r["phase"]),
            buddy_id=str(r["buddy_id"]) if r["buddy_id"] else None,
            welcomed=r["welcomed_at"] is not None,
        )

    def create_plan(self, plan_id: str, person_id: str, start: date, buddy_id: str | None) -> None:
        self._uow.execute(
            "INSERT INTO onboarding_plans (id, person_id, start_date, buddy_id) VALUES (:id, :p, :s, :b)",
            {"id": plan_id, "p": person_id, "s": start, "b": buddy_id},
        )
        self._uow.execute(
            "INSERT INTO onboarding_items (id, plan_id, person_id, template_id, category, title, target_ref, due_day, "
            "mandatory, verify_rule) SELECT :plan || ':' || id, :plan, :p, id, category, title, target_ref, due_day, "
            "mandatory, verify_rule FROM onboarding_templates",
            {"plan": plan_id, "p": person_id},
        )

    def mark_welcomed(self, plan_id: str) -> None:
        self._uow.execute(
            "UPDATE onboarding_plans SET welcomed_at = CURRENT_TIMESTAMP WHERE id = :id AND welcomed_at IS NULL",
            {"id": plan_id},
        )

    def beat_record(self, person_id: str) -> str | None:
        row = self._uow.fetchone("SELECT beat_record FROM onboarding_plans WHERE person_id = :p", {"p": person_id})
        if row is None or row["beat_record"] is None:
            return None
        return str(row["beat_record"])

    def write_beat_record(self, person_id: str, raw: str) -> None:
        self._uow.execute(
            "UPDATE onboarding_plans SET beat_record = :r WHERE person_id = :p", {"p": person_id, "r": raw}
        )

    def items(self, person_id: str) -> list[ItemRow]:
        rows = self._uow.fetchall(
            "SELECT id, category, title, target_ref, due_day, mandatory, status FROM onboarding_items "
            "WHERE person_id = :p ORDER BY due_day, id",
            {"p": person_id},
        )
        return [
            ItemRow(
                id=str(r["id"]),
                category=str(r["category"]),
                title=str(r["title"]),
                target_ref=str(r["target_ref"]) if r["target_ref"] else None,
                due_day=int(r["due_day"]),
                mandatory=bool(r["mandatory"]),
                status=str(r["status"]),
            )
            for r in rows
        ]

    def set_item_status(self, person_id: str, item_id: str, status: str, evidence: str | None) -> bool:
        before = self._uow.fetchone(
            "SELECT 1 AS ok FROM onboarding_items WHERE id = :id AND person_id = :p", {"id": item_id, "p": person_id}
        )
        if before is None:
            return False
        self._uow.execute(
            "UPDATE onboarding_items SET status = :s, evidence_ref = :e, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :id AND person_id = :p",
            {"id": item_id, "p": person_id, "s": status, "e": evidence},
        )
        return True

    def people_with_open_plans(self) -> list[str]:
        rows = self._uow.fetchall("SELECT person_id FROM onboarding_plans WHERE phase <> 'graduated' ORDER BY 1")
        return [str(r["person_id"]) for r in rows]

    def recent_hires(self, asof: date, since: date) -> list[str]:
        rows = self._uow.fetchall(
            "SELECT id FROM people WHERE status = 'active' AND external = FALSE AND hired_on IS NOT NULL "
            "AND hired_on <= :asof AND hired_on >= :since ORDER BY id",
            {"asof": asof, "since": since},
        )
        return [str(r["id"]) for r in rows]

    def buddy_candidates(self, exclude: list[str], hired_before: date, unit_id: str | None) -> list[Row]:
        """Active tenured colleagues. ``unit_id`` set means that unit only (``buddy.same_unit``)."""
        marks = ", ".join(f":x{i}" for i in range(len(exclude))) or "''"
        unit = "AND m.unit_id = :u" if unit_id is not None else ""
        params: dict[str, object] = {"h": hired_before, **{f"x{i}": x for i, x in enumerate(exclude)}}
        if unit_id is not None:
            params["u"] = unit_id
        return self._uow.fetchall(
            "SELECT DISTINCT p.id, p.name, p.grade FROM people p JOIN memberships m ON m.person_id = p.id "
            f"WHERE m.valid_to IS NULL AND p.status = 'active' AND p.id NOT IN ({marks}) "
            f"AND p.hired_on <= :h {unit} ORDER BY p.id",
            params,
        )


class InboxRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def add(self, msg_id: str, person_id: str, kind: str, title: str, body: str, ref: str | None, dedupe: str) -> bool:
        """Insert unless an entry with the same dedupe key exists. Returns True when inserted."""
        if self._uow.fetchone("SELECT 1 AS ok FROM inbox_messages WHERE dedupe_key = :k", {"k": dedupe}):
            return False
        self._uow.execute(
            "INSERT INTO inbox_messages (id, person_id, kind, title, body, ref, dedupe_key) "
            "VALUES (:id, :p, :k, :t, :b, :r, :d)",
            {"id": msg_id, "p": person_id, "k": kind, "t": title, "b": body, "r": ref, "d": dedupe},
        )
        return True

    def list(self, person_id: str, limit: int = 50) -> list[InboxRow]:
        rows = self._uow.fetchall(
            "SELECT id, kind, title, body, ref, created_at, read_at FROM inbox_messages WHERE person_id = :p "
            "ORDER BY created_at DESC, id DESC LIMIT :n",
            {"p": person_id, "n": limit},
        )
        return [
            InboxRow(
                id=str(r["id"]),
                kind=str(r["kind"]),
                title=str(r["title"]),
                body=str(r["body"]),
                ref=str(r["ref"]) if r["ref"] else None,
                created_at=str(r["created_at"]),
                read=r["read_at"] is not None,
            )
            for r in rows
        ]

    def mark_read(self, person_id: str, msg_id: str) -> None:
        self._uow.execute(
            "UPDATE inbox_messages SET read_at = CURRENT_TIMESTAMP WHERE id = :id AND person_id = :p",
            {"id": msg_id, "p": person_id},
        )
