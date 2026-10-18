"""The runtime login is nw_app: not the owner, and able to use PostgresSaver and PostgresStore."""

from __future__ import annotations

from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from nwb.access.visible import access_policy_for
from nwb.agent.persistence import build_persistence, prepare_agent_storage
from nwb.config import Settings
from nwb.db.app_role import enable_login
from nwb.db.database import PostgresDatabase
from nwb.errors import ConfigError
from tests.conftest import APP_ROLE_PASSWORD, PG_DSN, ROOT, app_dsn, fresh_db
from tests.contract.test_access_matrix import ASOF, EXPECT

pytestmark = pytest.mark.postgres


class _S(TypedDict):
    seen: Annotated[list[str], add]


def _graph(checkpointer: object):  # type: ignore[no-untyped-def]
    g = StateGraph(_S)
    g.add_node("echo", lambda s: {"seen": ["x"]})
    g.add_edge(START, "echo")
    g.add_edge("echo", END)
    return g.compile(checkpointer=checkpointer)  # type: ignore[arg-type]


def test_app_role_is_required_in_prod_and_cannot_create_tables(tmp_path: Path) -> None:
    owner = fresh_db("postgres", tmp_path)
    try:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            enable_login(conn, APP_ROLE_PASSWORD)
        with pytest.raises(ConfigError):
            PostgresDatabase(PG_DSN, max_size=1, require_app_role=True)
        app = PostgresDatabase(app_dsn(), max_size=1, require_app_role=True)
        try:
            with pytest.raises(psycopg.Error), app.unit_of_work() as uow:
                uow.execute("CREATE TABLE app_role_must_not_create (id int)")
        finally:
            app.close()
    finally:
        owner.close()


def test_access_matrix_holds_when_connected_as_the_app_role(tmp_path: Path) -> None:
    owner = fresh_db("postgres", tmp_path)
    try:
        with owner.unit_of_work() as uow:
            uow.executescript((ROOT / "tests" / "fixtures" / "seed_test.sql").read_text())
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            enable_login(conn, APP_ROLE_PASSWORD)
        app = PostgresDatabase(app_dsn(), max_size=1, require_app_role=True)
        try:
            policy = access_policy_for(app)
            for person, expected in EXPECT.items():
                assert policy.visible_record_ids(person, ASOF) == expected
        finally:
            app.close()
    finally:
        owner.close()


def test_checkpointer_and_store_round_trip_as_the_app_role(tmp_path: Path) -> None:
    owner = fresh_db("postgres", tmp_path)
    try:
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            enable_login(conn, APP_ROLE_PASSWORD)
        prepare_agent_storage(PG_DSN)
        settings = Settings(checkpointer="postgres", pg_dsn=app_dsn())  # type: ignore[call-arg]
        first = build_persistence(settings)
        cfg = {"configurable": {"thread_id": "session-app-role"}}
        _graph(first.checkpointer).invoke({"seen": []}, cfg)  # type: ignore[arg-type]
        first.store.put(("co", "per"), "note", {"text": "kept"})
        first.close()

        second = build_persistence(settings)
        try:
            state = _graph(second.checkpointer).get_state(cfg)  # type: ignore[arg-type]
            assert state.values["seen"] == ["x"]
            item = second.store.get(("co", "per"), "note")
            assert item is not None and item.value["text"] == "kept"
        finally:
            second.close()
    finally:
        owner.close()
