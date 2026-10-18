"""Migration 0019 removes the SQL facts table. Facts are graph nodes."""

from nwb.ports import Dialect


def test_sql_facts_table_is_dropped(db):
    with db.unit_of_work() as uow:
        if db.dialect is Dialect.SQLITE:
            facts = uow.fetchone("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'facts'")
            alerts = uow.fetchone("SELECT sql FROM sqlite_master WHERE name = 'alerts'")
            policy = None
        else:
            facts = uow.fetchone("SELECT to_regclass('public.facts') AS name")
            alerts = uow.fetchone(
                "SELECT pg_get_constraintdef(c.oid) AS sql FROM pg_constraint c "
                "WHERE c.conrelid = 'public.alerts'::regclass AND c.contype = 'f' "
                "AND pg_get_constraintdef(c.oid) ILIKE '%facts%'"
            )
            policy = uow.fetchone("SELECT polname FROM pg_policy WHERE polname = 'facts_read'")
    assert facts is None or facts["name"] is None
    assert alerts is None or "facts" not in str(alerts["sql"]).lower()
    assert policy is None
