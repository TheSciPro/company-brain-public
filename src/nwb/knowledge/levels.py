"""Which per-level company graphs a caller may query.

A2 stores one FalkorDB graph per classification level (``{company}_kb_{level}``).
GraphRAG-SDK ``retrieve`` has no per-row filter, so a graph is opened only when two
things are both true:

1. ``config/classification.yaml`` says the level is uniform: every tag that maps to
   it is neither grant-gated nor personal, and unmapped tags do not fail closed into
   it. Anything else is refused. A partial grant must not open the whole graph.
2. The access policy already shows this caller every row at that level. One invisible
   row closes the graph. An audience (who will see the reply) is the same check; the
   graphs queried are the intersection.

Empty levels are not queried. Unknown level codes are not queried. No count of the
rows that were left out is returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nwb.conf.models import ClassificationConfig
from nwb.identity import Actor
from nwb.ports import AccessPolicy, SqlDatabase

SkipReason = Literal["access", "audience", "empty"]


@dataclass(frozen=True, slots=True)
class LevelDecision:
    level: str
    query: bool
    reason: SkipReason | None = None


@dataclass(frozen=True, slots=True)
class ReadPlan:
    decisions: tuple[LevelDecision, ...]

    @property
    def allowed(self) -> tuple[str, ...]:
        return tuple(item.level for item in self.decisions if item.query)

    def skipped(self) -> tuple[LevelDecision, ...]:
        return tuple(item for item in self.decisions if not item.query)


def eligible_levels(cfg: ClassificationConfig) -> tuple[str, ...]:
    """Levels whose graph is uniform enough to open, lowest rank first.

    A level with no tags, a grant, a personal tag, or the unknown-tag sink is
    not eligible. Callers never see a graph we cannot prove is wholly readable.
    """
    by_level: dict[str, list[bool]] = {level.code: [] for level in cfg.levels}
    for rule in cfg.tags.values():
        by_level.setdefault(rule.level, []).append(rule.grants or rule.personal)
    unknown_closed = cfg.unknown_tag.grants or cfg.unknown_tag.personal
    ordered = sorted(cfg.levels, key=lambda level: (level.rank, level.code))
    chosen: list[str] = []
    for level in ordered:
        flags = by_level.get(level.code, [])
        if not flags or any(flags):
            continue
        if unknown_closed and level.code == cfg.unknown_tag.level:
            continue
        chosen.append(level.code)
    return tuple(chosen)


def plan_reads(
    db: SqlDatabase,
    policy: AccessPolicy,
    cfg: ClassificationConfig,
    actor: Actor,
    audience: Actor | None = None,
) -> ReadPlan:
    """The graphs to open for ``actor``, narrowed by ``audience`` when it differs."""
    eligible = eligible_levels(cfg)
    inventory = _inventory(db, actor.company_id, set(eligible))
    reader = _visible(policy, actor)
    others = None
    if audience is not None and audience.person_id != actor.person_id:
        others = _visible(policy, audience)
    order = {level.code: index for index, level in enumerate(sorted(cfg.levels, key=lambda lv: (lv.rank, lv.code)))}
    decisions: list[LevelDecision] = []
    eligible_set = set(eligible)
    for level in cfg.levels:
        if level.code not in eligible_set:
            decisions.append(LevelDecision(level.code, False, "access"))
            continue
        ids = inventory.get(level.code, set())
        if not ids:
            decisions.append(LevelDecision(level.code, False, "empty"))
            continue
        if not ids <= reader:
            decisions.append(LevelDecision(level.code, False, "access"))
            continue
        if others is not None and not ids <= others:
            decisions.append(LevelDecision(level.code, False, "audience"))
            continue
        decisions.append(LevelDecision(level.code, True, None))
    decisions.sort(key=lambda item: order.get(item.level, len(order)))
    return ReadPlan(tuple(decisions))


def _inventory(db: SqlDatabase, company_id: str, levels: set[str]) -> dict[str, set[str]]:
    """Every document and record id at an eligible level. Ids are namespaced so a
    document and a record that share a business id stay distinct.

    This read is unscoped on purpose: it has to see rows the caller cannot, so it
    can refuse the graph. It must run as a role that is not under row-level
    security. The ids are not returned to the caller.
    """
    grouped: dict[str, set[str]] = {level: set() for level in levels}
    if not company_id or not levels:
        return grouped
    with db.unit_of_work() as uow:
        docs = uow.fetchall(
            "SELECT id, classification FROM documents WHERE company_id = :c AND deleted_at IS NULL",
            {"c": company_id},
        )
        recs = uow.fetchall(
            "SELECT id, classification FROM records WHERE company_id = :c",
            {"c": company_id},
        )
    for prefix, rows in (("d", docs), ("r", recs)):
        for row in rows:
            level = str(row["classification"])
            if level in grouped:
                grouped[level].add(f"{prefix}:{row['id']}")
    return grouped


def _visible(policy: AccessPolicy, actor: Actor) -> set[str]:
    if not actor.person_id:
        return set()
    docs = policy.visible_document_ids(actor.person_id, actor.asof)
    recs = policy.visible_record_ids(actor.person_id, actor.asof)
    return {f"d:{item}" for item in docs} | {f"r:{item}" for item in recs}
