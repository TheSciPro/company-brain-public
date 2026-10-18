"""Layer 2 on real Northwind data: precedence traps T1, T2, T3, T6, T7, T10, idempotency, grants, graph."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from nwb.access.visible import access_policy_for
from nwb.blob.local import LocalBlobStore
from nwb.ingest.layer2 import Layer2Loader
from nwb.ports import SqlDatabase
from nwb.temporal.graph_facts import GraphFactBook
from nwb.temporal.precedence import PrecedenceEngine
from tests.conftest import DATA, cached_extractor, fresh_db, shipped_sources
from tests.integration.test_layer1 import loader as layer1

D = date.fromisoformat
NIKHIL, AUDITOR, PRICING = "NW1103", "EXT-AA-01", "NW0202"


def fact_book(db: SqlDatabase, tmp: Path) -> GraphFactBook:
    book = getattr(db, "facts", None)
    if book is None:
        book = GraphFactBook.open_lite(tmp / "facts.falkor", company="NW")
        db.facts = book  # type: ignore[attr-defined]
    return book


def layer2(db: SqlDatabase, tmp: Path) -> Layer2Loader:
    src = shipped_sources()
    return Layer2Loader(
        db,
        DATA,
        LocalBlobStore(tmp / "blobs"),
        "NW",
        src,
        cached_extractor(),
        "Northwind Consulting",
        src.directory.root_unit,
        fact_book(db, tmp),
        "highly_confidential",
    )


@pytest.fixture(scope="module", params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def loaded(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[SqlDatabase]:
    tmp = tmp_path_factory.mktemp("l2")
    db = fresh_db(request.param, tmp)
    layer1(db).load()
    rep = layer2(db, tmp).load()
    # The quote check is the guard against model paraphrase: rejected facts are logged, never stored, and rare.
    assert rep.rejected <= 0.15 * (sum(rep.facts.values()) + rep.rejected)
    with db.unit_of_work() as uow:
        logged = uow.fetchall("SELECT reason FROM ingest_log WHERE source = 'facts' AND status = 'rejected'")
    assert len(logged) == rep.rejected and all(str(r["reason"]).startswith("quote_mismatch") for r in logged)
    db.tmp = tmp  # type: ignore[attr-defined]
    yield db
    book = getattr(db, "facts", None)
    if book is not None:
        book.release()
    db.close()


def answer(db: SqlDatabase, key: str, about: str, asof: str, scope: str | None = None, person: str | None = None):
    src_ids = None
    if person:
        policy = access_policy_for(db)
        src_ids = set(policy.visible_document_ids(person, D(asof))) | set(policy.visible_record_ids(person, D(asof)))
    return PrecedenceEngine(fact_book(db, db.tmp)).as_of(key, about, D(asof), scope=scope, src_ids=src_ids)  # type: ignore[attr-defined]


def key_of(db: SqlDatabase, src_id: str, value: str) -> str:
    """The key extraction gave ``value`` in ``src_id``: the vocabulary is discovered, never named in a test."""
    found = fact_book(db, db.tmp).find_key(src_id, value)  # type: ignore[attr-defined]
    assert found is not None, (src_id, value)
    return found


BRM = "client=ACC-BRM"


def test_t1_draft_discount_never_wins(loaded):
    k = key_of(loaded, "DEC-2026-005", "18")
    for asof in ("2026-02-01", "2026-09-28"):
        ans = answer(loaded, k, "NW", asof, scope=BRM)
        assert ans.fact is None or ans.fact.value == "18"
    ans = answer(loaded, k, "NW", "2026-09-28", scope=BRM)
    assert ans.fact is not None and ans.fact.value == "18"
    assert any(c.value == "12" and c.status == "draft" for c in ans.contested)


def test_t2_draft_policy_4_days_never_effective(loaded):
    k, india = key_of(loaded, "POL-HR-WFH@v3", "3"), "country=IN"
    ans = answer(loaded, k, "NW", "2026-03-25", scope=india)
    assert ans.fact is not None and ans.fact.value == "2"  # v2 still in force; rc1 draft ignored
    ans = answer(loaded, k, "NW", "2026-09-28", scope=india)
    assert ans.fact is not None and ans.fact.value == "3"
    assert ans.replaced is not None and ans.replaced.value == "2"
    assert [c.value for c in ans.contested if c.status == "draft"] == ["4"]


def test_t3_client_24h_overrides_policy_72h(loaded):
    k = key_of(loaded, "POL-SEC-IR@v2", "72")
    brm = answer(loaded, k, "NW", "2026-08-02", scope=BRM)
    assert brm.fact is not None and brm.fact.value == "24"
    assert brm.overridden is not None and brm.overridden.value == "72"  # flagged: policy default conflicts
    general = answer(loaded, k, "NW", "2026-08-02")
    assert general.fact is not None and general.fact.value == "72"


def test_t6_approver_follows_reporting_line_as_of_request_date(loaded):
    q = (
        "SELECT manager_id FROM reporting_lines WHERE person_id = 'NW1101' AND valid_from <= :d "
        "AND (valid_to IS NULL OR valid_to >= :d)"
    )
    with loaded.unit_of_work() as uow:
        before = uow.fetchone(q, {"d": D("2026-04-15")})
        after = uow.fetchone(q, {"d": D("2026-06-01")})
    assert before == {"manager_id": "NW0011"}  # Rahul before ORG-2026-05
    assert after == {"manager_id": "NW0013"}  # Eleanor since 1 May


# T7 (contract beats policy under a *different* extracted key) is a knowledge-service property, not a single-key
# precedence one: see tests/agent/test_heldout_knowledge.py::test_client_contract_overrides_policy_for_allocated_staff.


def test_t10_policy_version_in_force_at_event_time(loaded):
    k = key_of(loaded, "POL-RMG-BENCH@v2", "10")
    feb = answer(loaded, k, "NW", "2026-02-19")
    assert feb.fact is not None and (feb.fact.value, feb.fact.src_id) == ("10", "POL-RMG-BENCH@v2")
    now = answer(loaded, k, "NW", "2026-09-28")
    assert now.fact is not None and now.fact.value == "15" and now.replaced is not None


def test_facts_inherit_source_visibility(loaded):
    # Nikhil (PM, not in the pricing ACL) must not see the 18% discount; the pricing desk can.
    k = key_of(loaded, "DEC-2026-005", "18")
    hidden = answer(loaded, k, "NW", "2026-09-28", scope=BRM, person=NIKHIL)
    assert hidden.fact is None or hidden.fact.value != "18"
    seen = answer(loaded, k, "NW", "2026-09-28", scope=BRM, person=PRICING)
    assert seen.fact is not None and seen.fact.value == "18"


def test_auditor_contract_window(loaded):
    policy = access_policy_for(loaded)
    assert "MSA-BRM-2026" in policy.visible_document_ids(AUDITOR, D("2026-09-28"))
    assert "MSA-BRM-2026" not in policy.visible_document_ids(AUDITOR, D("2026-11-02"))


def test_personal_records_owner_only(loaded):
    with loaded.unit_of_work() as uow:
        row = uow.fetchone("SELECT id, owner_person_id FROM records WHERE personal ORDER BY id LIMIT 1")
    assert row is not None
    policy = access_policy_for(loaded)
    assert policy.can_read_record(row["owner_person_id"], D("2026-09-28"), row["id"])
    assert not policy.can_read_record(
        NIKHIL if row["owner_person_id"] != NIKHIL else "NW0011", D("2026-09-28"), row["id"]
    )


def test_reload_skips_unchanged(loaded):
    rep = layer2(loaded, loaded.tmp).load()  # type: ignore[attr-defined]
    assert (rep.records_written, rep.documents_written) == (0, 0)
