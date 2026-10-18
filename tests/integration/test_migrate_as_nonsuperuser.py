"""Migrations must apply as a non-superuser owner.

Neon, Supabase and RDS give the migrating role CREATEROLE without SUPERUSER or BYPASSRLS.
``ALTER ROLE ... NOSUPERUSER`` is denied for that role, which is what rolled 0017 back on Neon.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql

from nwb.db.database import PostgresDatabase
from nwb.db.migrate import Migrator
from tests.conftest import MIGRATIONS, PG_DSN, PG_OK, reset_pg

pytestmark = pytest.mark.postgres

_OWNER = "nwb_owner"
# Local fixture password. Not a deployed secret; never log it.
_OWNER_PASSWORD = "nwb-owner-migrate"


def _owner_dsn() -> str:
    parsed = urlparse(PG_DSN)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    path = parsed.path or "/nwb_test"
    netloc = f"{quote(_OWNER)}:{quote(_OWNER_PASSWORD)}@{host}:{port}"
    return urlunparse((parsed.scheme or "postgresql", netloc, path, "", parsed.query, ""))


def _postgres_migration_names() -> list[str]:
    names: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name.endswith(".sqlite.sql"):
            continue
        names.append(path.name)
    return names


def test_migrations_apply_as_nonsuperuser_owner() -> None:
    if not PG_OK:
        pytest.skip(f"Postgres not reachable at {PG_DSN}")
    db: PostgresDatabase | None = None
    try:
        with psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin_row = admin.execute("SELECT current_user AS u").fetchone()
            assert admin_row is not None
            admin_name = str(admin_row[0])
            admin.execute(
                sql.SQL(
                    """
                    DO $$ BEGIN
                      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role}) THEN
                        CREATE ROLE nwb_owner LOGIN PASSWORD {pw}
                          NOSUPERUSER NOCREATEDB CREATEROLE NOBYPASSRLS;
                      ELSE
                        ALTER ROLE nwb_owner LOGIN PASSWORD {pw}
                          NOSUPERUSER NOCREATEDB CREATEROLE NOBYPASSRLS;
                      END IF;
                    END $$
                    """
                ).format(role=sql.Literal(_OWNER), pw=sql.Literal(_OWNER_PASSWORD))
            )
            admin.execute("DROP SCHEMA IF EXISTS public CASCADE")
            exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nw_app'").fetchone()
            if exists:
                admin.execute("DROP OWNED BY nw_app CASCADE")
                admin.execute("DROP ROLE nw_app")
            admin.execute(sql.SQL("CREATE SCHEMA public AUTHORIZATION {}").format(sql.Identifier(_OWNER)))
            # A function this owner does not own. ALL FUNCTIONS would fail the migration.
            admin.execute("CREATE FUNCTION public.other_owner_fn() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$")

        db = PostgresDatabase(_owner_dsn(), max_size=1)
        ran = Migrator(db, MIGRATIONS).migrate()
        assert ran
        with db.unit_of_work() as uow:
            applied = {str(r["name"]) for r in uow.fetchall("SELECT name FROM schema_migrations")}
            role = uow.fetchone(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname = 'nw_app'"
            )
            definers = uow.fetchall(
                "SELECT p.proname, pg_get_userbyid(p.proowner) AS owner FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.prosecdef ORDER BY p.proname"
            )
            decoy = uow.fetchone(
                "SELECT pg_get_userbyid(p.proowner) AS owner FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'other_owner_fn'"
            )
            documents = uow.fetchone(
                "SELECT pg_get_userbyid(c.relowner) AS owner FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'documents'"
            )
            created = uow.fetchone(
                "SELECT m.admin_option FROM pg_auth_members m "
                "JOIN pg_roles granted ON granted.oid = m.roleid "
                "JOIN pg_roles member ON member.oid = m.member "
                "WHERE granted.rolname = 'nw_app' AND member.rolname = 'nwb_owner'"
            )
        assert applied == set(_postgres_migration_names())
        assert role is not None
        assert role["rolsuper"] is False
        assert role["rolcreatedb"] is False
        assert role["rolcreaterole"] is False
        assert role["rolbypassrls"] is False
        assert role["rolcanlogin"] is False
        assert {str(r["proname"]) for r in definers} >= {
            "nw_can_read",
            "nw_same_company",
            "nw_is_approver",
            "nw_owns_action",
            "nw_action_requester",
        }
        assert {str(r["owner"]) for r in definers} == {_OWNER}
        assert decoy is not None and decoy["owner"] == admin_name
        assert documents is not None and documents["owner"] == _OWNER
        assert created is not None and created["admin_option"] is True

        with psycopg.connect(_owner_dsn(), autocommit=True) as owner, pytest.raises(psycopg.Error, match="SUPERUSER"):
            owner.execute("ALTER ROLE nw_app NOSUPERUSER")
    finally:
        if db is not None:
            db.close()
        if PG_OK:
            reset_pg()
