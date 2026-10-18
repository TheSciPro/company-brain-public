"""User-scoped rows are visible only to that person, and only inside their company."""

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
INSERT INTO people (id, company_id, name) VALUES ('per-a', 'co-a', 'Person A'), ('per-b', 'co-b', 'Person B');
INSERT INTO sessions (id, person_id) VALUES ('s-a', 'per-a'), ('s-b', 'per-b');
"""


def test_rls_isolates_sessions_by_person_and_company(tmp_path: Path) -> None:
    owner = fresh_db("postgres", tmp_path)
    try:
        with owner.unit_of_work() as uow:
            uow.executescript(_SEED)
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            enable_login(conn, APP_ROLE_PASSWORD)
        app = PostgresDatabase(app_dsn(), max_size=1, require_app_role=True)
        try:
            with app.unit_of_work(as_person="per-a") as uow:
                rows = uow.fetchall("SELECT id FROM sessions ORDER BY id")
            assert [r["id"] for r in rows] == ["s-a"]

            with app.unit_of_work(as_person="per-a", as_company="co-b") as uow:
                mismatch = uow.fetchone("SELECT count(*) AS n FROM sessions")
            assert mismatch is not None and mismatch["n"] == 0

            with app.unit_of_work(as_person="per-a", as_company="") as uow:
                unset = uow.fetchone("SELECT count(*) AS n FROM sessions")
            assert unset is not None and unset["n"] == 0

            with app.unit_of_work() as uow:
                anonymous = uow.fetchone("SELECT count(*) AS n FROM sessions")
            assert anonymous is not None and anonymous["n"] == 0

            with pytest.raises(psycopg.Error), app.unit_of_work(as_person="per-a") as uow:
                uow.execute("INSERT INTO sessions (id, person_id) VALUES ('s-x', 'per-b')")
        finally:
            app.close()
    finally:
        owner.close()
