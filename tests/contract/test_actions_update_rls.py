"""An actions update cannot reassign person_id. WITH CHECK repeats the update rule and keeps the requester."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from nwb.db.app_role import enable_login
from nwb.db.database import PostgresDatabase
from tests.conftest import APP_ROLE_PASSWORD, PG_DSN, app_dsn, fresh_db

pytestmark = pytest.mark.postgres

_SEED = """
INSERT INTO companies (id, name) VALUES ('co-a', 'Company A'), ('co-b', 'Company B');
INSERT INTO people (id, company_id, name) VALUES
  ('per-a', 'co-a', 'Requester'),
  ('per-b', 'co-a', 'Approver'),
  ('per-c', 'co-a', 'Colleague'),
  ('per-x', 'co-b', 'Outsider');
INSERT INTO actions (id, person_id, action_type, status) VALUES
  ('act-1', 'per-a', 'flag', 'awaiting_approval');
INSERT INTO approvals (id, action_id, approver_id) VALUES ('apr-1', 'act-1', 'per-b');
"""


def _person(app: PostgresDatabase, person_id: str) -> tuple[str, str]:
    with app.unit_of_work(as_person=person_id) as uow:
        row = uow.fetchone("SELECT person_id, status FROM actions WHERE id = 'act-1'")
    assert row is not None
    return str(row["person_id"]), str(row["status"])


def test_actions_update_cannot_reassign_the_requester(tmp_path: Path) -> None:
    owner = fresh_db("postgres", tmp_path)
    try:
        with owner.unit_of_work() as uow:
            uow.executescript(_SEED)
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            enable_login(conn, APP_ROLE_PASSWORD)
        app = PostgresDatabase(app_dsn(), max_size=1, require_app_role=True)
        try:
            with app.unit_of_work(as_person="per-b") as uow:
                uow.execute("UPDATE actions SET status = 'approved' WHERE id = 'act-1'")
            assert _person(app, "per-a") == ("per-a", "approved")

            for actor, new_person in (("per-a", "per-c"), ("per-b", "per-c"), ("per-b", "per-b")):
                with pytest.raises(psycopg.Error, match="row-level security"), app.unit_of_work(as_person=actor) as uow:
                    uow.execute(
                        "UPDATE actions SET person_id = :p WHERE id = 'act-1'",
                        {"p": new_person},
                    )
            assert _person(app, "per-a") == ("per-a", "approved")

            with app.unit_of_work(as_person="per-c") as uow:
                uow.execute("UPDATE actions SET person_id = 'per-c', status = 'done' WHERE id = 'act-1'")
            assert _person(app, "per-a") == ("per-a", "approved")

            for actor, expected in (("per-a", "per-a"), ("per-b", "per-a"), ("per-c", None), ("per-x", None)):
                with app.unit_of_work(as_person=actor) as uow:
                    row = uow.fetchone("SELECT nw_action_requester('act-1') AS who")
                assert row is not None and row["who"] == expected
            with app.unit_of_work() as uow:
                row = uow.fetchone("SELECT nw_action_requester('act-1') AS who")
            assert row is not None and row["who"] is None
        finally:
            app.close()
    finally:
        owner.close()
