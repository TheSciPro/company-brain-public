"""Held-out knowledge questions (audit Part E.2). None of these were ever demo scenarios.

Answers must come from facts *extracted* from the documents (recorded LLM extractions replayed from
``tests/fixtures/extractions``), resolved from natural-language questions, scoped by who is asking.
"""

from __future__ import annotations

import csv
from datetime import date

import pytest

from tests.agent.conftest import Harness
from tests.conftest import DATA


def _person(h: Harness, *, country: str, with_client: bool | None) -> str:
    """Someone in ``country`` whose client allocations match ``with_client`` (None = don't care)."""
    with h.c.db.unit_of_work() as uow:
        ids = [
            str(r["id"]) for r in uow.fetchall("SELECT id FROM people WHERE country = :c ORDER BY id", {"c": country})
        ]
    for pid in ids:
        scopes = h.c.knowledge.scopes_for(pid, date(2026, 9, 28))
        if with_client is None or bool(scopes.get("client")) == with_client:
            return pid
    raise AssertionError(f"no person in {country} with_client={with_client}")


@pytest.mark.parametrize(
    ("country", "question", "value", "cite"),
    [
        ("GB", "How many days a week can I work from home?", "3", "POL-HR-WFH@v3"),
        ("IN", "What is the minimum number of anchor days I must be in the office each week?", "2", "POL-HR-WFH@v3"),
        ("IN", "How many work-from-anywhere days do I get per year?", "20", "POL-HR-WFH@v3"),
    ],
)
def test_heldout_policy_questions(harness: Harness, country: str, question: str, value: str, cite: str) -> None:
    pid = _person(harness, country=country, with_client=False)
    res = harness.c.knowledge.lookup(harness.actor(pid), question)
    assert res.best is not None, res
    assert res.best.answer is not None
    assert res.best.answer.value == value
    assert res.best.answer.citation.id == cite


def test_client_contract_overrides_policy_for_allocated_staff(harness: Harness) -> None:
    pid = _person(harness, country="GB", with_client=True)
    res = harness.c.knowledge.lookup(harness.actor(pid), "How many days per week can I work from home?")
    assert res.best is not None and res.best.answer is not None
    assert res.best.answer.citation.authority == "contract"
    assert res.best.overridden_general is not None


def test_as_of_dates_follow_supersession(harness: Harness) -> None:
    actor = harness.actor("NW1103")
    q = "How many working days of handover are required before releasing someone from a project?"
    before = harness.c.knowledge.lookup(actor, q, on_date=date(2026, 5, 1)).best
    after = harness.c.knowledge.lookup(actor, q, on_date=date(2026, 9, 28)).best
    assert before is not None and after is not None and before.answer and after.answer
    assert before.answer.value != after.answer.value
    assert after.replaced is not None


def test_draft_never_wins(harness: Harness) -> None:
    res = harness.c.knowledge.lookup(harness.actor("NW0011"), "What discount did we agree?", client="Brightmart")
    assert res.best is not None and res.best.answer is not None
    assert res.best.answer.value == "18"
    assert any(c.status == "draft" for c in res.best.contested)


def _doa_rows() -> list[dict[str, str]]:
    with (DATA / "policies" / "delegation_of_authority_matrix.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    return [r for r in rows if r["region"] != "ALL" and r["min"] and r["max"]]


@pytest.mark.parametrize("row", _doa_rows(), ids=lambda r: r["id"])
def test_every_regional_doa_band_resolves(harness: Harness, row: dict[str, str]) -> None:
    """Every regional, bounded row resolves from its own words + an amount inside the band (no row is special)."""
    from nwb.util.geo import normalize_country

    pid = _person(harness, country=normalize_country(row["region"]), with_client=None)
    amount = (float(row["min"]) + float(row["max"])) / 2
    ans = harness.c.approvals.who_approves(harness.actor(pid), row["item"], amount, None)
    assert ans.rule is not None, ans.note
    assert ans.rule.rule_id == row["id"]
