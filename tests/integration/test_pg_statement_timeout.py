"""The Postgres statement timeout is set per transaction, so a transaction-mode pooler (Neon) accepts the login."""

from __future__ import annotations

import pytest

from nwb.db.database import PostgresDatabase
from tests.conftest import PG_DSN, PG_OK

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not PG_OK, reason="no local Postgres")]


def test_statement_timeout_is_set_in_each_transaction_not_at_login() -> None:
    db = PostgresDatabase(PG_DSN, max_size=1, statement_timeout_ms=1234)
    try:
        with db.unit_of_work() as uow:
            row = uow.fetchone("SELECT current_setting('statement_timeout') AS t")
            assert row is not None and row["t"] == "1234ms"
        with db.unit_of_work() as uow:
            again = uow.fetchone("SELECT current_setting('statement_timeout') AS t")
            assert again is not None and again["t"] == "1234ms"
    finally:
        db.close()
    # No startup ``options`` parameter: PgBouncer in transaction mode rejects one.
    assert "options" not in db._pool.kwargs
