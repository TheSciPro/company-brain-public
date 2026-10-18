"""Layer 1 on real Northwind data: loader, idempotency, dated org facts and access with real personas."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from nwb.access.visible import access_policy_for
from nwb.db.models import Company
from nwb.db.repo_org import OrgRepository
from nwb.ingest.layer1 import Layer1Loader
from nwb.ingest.sources import DirectorySource, HrisSource
from nwb.ports import SqlDatabase
from tests.conftest import DATA, fresh_db, shipped_sources

RAHUL, NIKHIL, OMKAR, CLAIRE = "NW0011", "NW1103", "NW2027", "EXT-AA-01"


def loader(db: SqlDatabase) -> Layer1Loader:
    return Layer1Loader(
        db,
        DirectorySource(DATA),
        HrisSource(DATA),
        Company(id="NW", name="Northwind Consulting"),
        shipped_sources().directory,
    )


@pytest.fixture(scope="module", params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def loaded(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[SqlDatabase]:
    db = fresh_db(request.param, tmp_path_factory.mktemp("l1"))
    loader(db).load()
    _seed_records(db)
    yield db
    db.close()


def _seed_records(db: SqlDatabase) -> None:
    """Synthetic test records on real units; grants derived from ACL tag subjects (what M2 ingestion will do)."""
    rows = [
        ("t-int", "CON", "internal", None, "PUBLIC-INT"),
        ("t-con-team", "CON", "confidential", "team", None),
        ("t-con-lead", "CON", "confidential", "leadership", "MGR"),
        ("t-rtl-team", "RTL", "confidential", "team", None),
        ("t-brm", "CON", "highly_confidential", None, "CLIENT-CONF-BRM"),
    ]
    with db.unit_of_work() as uow:
        for rid, unit, cls, share, tag in rows:
            uow.execute(
                "INSERT INTO records (id, company_id, source, source_id, record_type, owner_unit_id, dept_id, "
                "classification, share_scope, source_tag, payload, hash) VALUES "
                "(:id, 'NW', 'test', :id, 'doc', :u, :u, :c, :s, :t, '{}', :id)",
                {"id": rid, "u": unit, "c": cls, "s": share, "t": tag},
            )
        subjects = uow.fetchall(
            "SELECT subject_type, subject_id FROM access_rule_subjects WHERE rule_id = 'NW:CLIENT-CONF-BRM'"
        )
        for i, s in enumerate(subjects):
            uow.execute(
                "INSERT INTO grants (id, company_id, subject_type, subject_id, resource_type, resource_id, valid_from) "
                "VALUES (:id, 'NW', :t, :s, 'record', 't-brm', '2025-01-01')",
                {"id": f"g-brm-{i}", "t": s["subject_type"], "s": s["subject_id"]},
            )


def test_counts(loaded: SqlDatabase):
    with loaded.unit_of_work() as uow:
        repo = OrgRepository(uow)
        assert repo.count("people") == 3002
        assert repo.count("groups") == 22
        assert repo.count("access_rules") == 13
        assert repo.count("memberships") > 3000
        assert repo.count("org_unit_closure") == 15 + 14  # self pairs + each dept under ORG


def test_reorg_is_dated(loaded: SqlDatabase):
    with loaded.unit_of_work() as uow:
        rows = uow.fetchall(
            "SELECT unit_id, valid_from, valid_to FROM memberships WHERE person_id = :p AND is_primary ORDER BY valid_from",
            {"p": RAHUL},
        )
    assert [(r["unit_id"], str(r["valid_from"]), r["valid_to"] and str(r["valid_to"])) for r in rows] == [
        ("RTL", "2014-08-18", "2026-04-30"),
        ("CON", "2026-05-01", None),
    ]


def test_new_hire_has_membership_and_manager(loaded: SqlDatabase):
    with loaded.unit_of_work() as uow:
        m = uow.fetchone("SELECT unit_id FROM memberships WHERE person_id = :p", {"p": OMKAR})
        r = uow.fetchone(
            "SELECT manager_id FROM reporting_lines WHERE person_id = :p AND valid_to IS NULL", {"p": OMKAR}
        )
    assert m == {"unit_id": "BFS"} and r == {"manager_id": "NW2075"}


@pytest.mark.parametrize(
    ("person", "asof", "expected"),
    [
        (NIKHIL, "2026-09-28", ["t-brm", "t-con-team", "t-int"]),  # CON member since transfer; Brightmart team grant
        (NIKHIL, "2026-08-01", ["t-brm", "t-int"]),  # still in BFS: no CON team docs
        (RAHUL, "2026-09-28", ["t-con-lead", "t-con-team", "t-int"]),  # leads CON; not on the BRM grant list
        (RAHUL, "2026-04-01", ["t-int", "t-rtl-team"]),  # before reorg: RTL
        (CLAIRE, "2026-09-28", ["t-brm"]),  # auditor: grant only, no membership -> no internal
        (CLAIRE, "2026-11-02", []),  # time-box expired 2026-10-30
        (OMKAR, "2026-09-28", ["t-int"]),  # new hire in BFS
    ],
)
def test_access_real_personas(loaded: SqlDatabase, person: str, asof: str, expected: list[str]):
    got = access_policy_for(loaded).visible_record_ids(person, date.fromisoformat(asof))
    assert got == expected


def test_reload_is_idempotent(tmp_path: Path):
    db = fresh_db("sqlite", tmp_path)
    first = loader(db).load()
    second = loader(db).load()
    assert first == second
    with db.unit_of_work() as uow:
        assert OrgRepository(uow).count("memberships") == first.memberships
