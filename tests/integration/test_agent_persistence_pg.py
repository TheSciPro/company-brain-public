"""Agent persistence on Postgres (the production path: PostgresSaver + PostgresStore on DATABASE_URL)."""

from __future__ import annotations

from datetime import date
from operator import add
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from nwb.agent.memory_files import MemoryFiles
from nwb.agent.persistence import build_persistence
from nwb.config import Settings
from tests.conftest import PG_DSN, ROOT, fresh_db
from tests.integration.test_layer1 import loader

pytestmark = pytest.mark.postgres


class _S(TypedDict):
    seen: Annotated[list[str], add]


def _graph(checkpointer: object):  # type: ignore[no-untyped-def]
    g = StateGraph(_S)
    g.add_node("echo", lambda s: {"seen": ["x"]})
    g.add_edge(START, "echo")
    g.add_edge("echo", END)
    return g.compile(checkpointer=checkpointer)  # type: ignore[arg-type]


def test_threads_and_memory_files_survive_a_reconnect(tmp_path: Path) -> None:
    db = fresh_db("postgres", tmp_path)  # skips when Postgres is unreachable; resets the schema
    loader(db).load()
    s = Settings(checkpointer="postgres", pg_dsn=PG_DSN)  # type: ignore[call-arg]
    assert s.agent_persistence == "postgres"

    p1 = build_persistence(s)
    cfg = {"configurable": {"thread_id": "session-1"}}
    _graph(p1.checkpointer).invoke({"seen": []}, cfg)  # type: ignore[arg-type]
    files = MemoryFiles(p1.store, db, ROOT / "agent" / "templates")
    assert files.ensure("NW", "NW1103", date(2026, 9, 28))
    p1.close()

    p2 = build_persistence(s)
    state = _graph(p2.checkpointer).get_state(cfg)  # type: ignore[arg-type]
    assert state.values["seen"] == ["x"]
    files2 = MemoryFiles(p2.store, db, ROOT / "agent" / "templates")
    assert "NW1103" in (files2.read("NW", "NW1103", "USER.md") or "")
    assert files2.read("NW", "NW0201", "USER.md") is None  # namespaced per (company, user)
    assert files2.ensure("NW", "NW1103", date(2026, 9, 28)) == []  # idempotent, never overwrites
    p2.close()
    db.close()
