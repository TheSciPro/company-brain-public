"""The non-owner SQL role the app connects as.

``nw_app`` is created ``NOLOGIN`` by the RLS migration. An operator turns login on with
``scripts/enable_app_role.py`` (password from ``NWB_APP_PASSWORD``, never from this file).
The role has no superuser bit, no ``BYPASSRLS``, and must not own tables: row-level security
only applies to a role that is not the owner and cannot bypass it.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from nwb.errors import ConfigError

APP_ROLE = "nw_app"

# LangGraph PostgresSaver / PostgresStore. Created by setup() on the owner connection.
AGENT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "store_migrations",
    "store",
)


def assert_app_role(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Refuse to serve traffic when this connection is the owner, a superuser, or can bypass RLS."""
    row = conn.execute(
        """
        SELECT current_user AS u, r.rolsuper AS super, r.rolbypassrls AS bypass,
               EXISTS (
                 SELECT 1 FROM pg_class c
                 JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relkind = 'r' AND pg_get_userbyid(c.relowner) = current_user
               ) AS owns
        FROM pg_roles r
        WHERE r.rolname = current_user
        """
    ).fetchone()
    if row is None or row["u"] != APP_ROLE or row["super"] or row["bypass"] or row["owns"]:
        raise ConfigError(
            "DATABASE_URL must log in as the non-owner role nw_app "
            "(not a superuser, not BYPASSRLS, not the owner of tables in public). "
            "Apply migrations with NWB_MIGRATE_URL, then enable login for nw_app."
        )


def enable_login(conn: psycopg.Connection[Any], password: str) -> None:
    """Let ``nw_app`` log in. The password is bound as a literal, never interpolated or logged."""
    conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(APP_ROLE), sql.Literal(password))
    )


def grant_agent_storage(conn: psycopg.Connection[Any]) -> None:
    """DML on the LangGraph tables. The app role must not be able to CREATE them."""
    for name in AGENT_TABLES:
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                sql.Identifier(name), sql.Identifier(APP_ROLE)
            )
        )


def agent_tables_ready(conn: psycopg.Connection[dict[str, Any]]) -> bool:
    rows = conn.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        """,
        (list(AGENT_TABLES),),
    ).fetchall()
    return {str(r["table_name"]) for r in rows} == set(AGENT_TABLES)
