"""Same access matrix on both backends."""

from datetime import date

import pytest

from nwb.access.visible import access_policy_for
from nwb.ports import Dialect, SqlDatabase
from tests.conftest import ROOT

EXPECT = {
    "nikhil": ["r-ai", "r-dm", "r-int", "r-pub"],
    "dana": ["r-data", "r-int", "r-pub"],
    "arjun": ["r-budget", "r-int", "r-pub"],
    "fiona": ["r-int", "r-pub", "r-rate"],
    "nlp1": ["r-ai", "r-int", "r-pub"],
}
ASOF = date(2026, 9, 28)


@pytest.fixture
def seeded(db: SqlDatabase) -> SqlDatabase:
    with db.unit_of_work() as uow:
        uow.executescript((ROOT / "tests" / "fixtures" / "seed_test.sql").read_text())
    return db


@pytest.mark.parametrize(("person", "expected"), EXPECT.items())
def test_access_matrix(seeded: SqlDatabase, person: str, expected: list[str]):
    assert access_policy_for(seeded).visible_record_ids(person, ASOF) == expected


def test_before_membership_only_public_and_own_personal(seeded: SqlDatabase):
    # memberships start 2026-01-01: no membership -> internal hidden; personal still own
    assert access_policy_for(seeded).visible_record_ids("nikhil", date(2025, 12, 31)) == ["r-dm", "r-pub"]


def test_grant_respects_dates(seeded: SqlDatabase):
    policy = access_policy_for(seeded)
    assert policy.can_read_record("fiona", ASOF, "r-rate")
    assert not policy.can_read_record("fiona", date(2025, 12, 31), "r-rate")


def test_unknown_person_sees_nothing(seeded: SqlDatabase):
    assert access_policy_for(seeded).visible_record_ids("intruder", ASOF) == []


@pytest.mark.postgres
def test_rls_blocks_direct_reads_on_postgres(seeded: SqlDatabase):
    if seeded.dialect is not Dialect.POSTGRES:
        pytest.skip("RLS is Postgres-only")
    with seeded.unit_of_work(as_person="dana", asof=ASOF) as uow:
        row = uow.fetchone("SELECT count(*) AS n FROM records WHERE owner_unit_id = 'AI'")
    assert row is not None and row["n"] == 0
