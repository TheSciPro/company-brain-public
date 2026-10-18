"""0018 drops the SQL memory tables only after nwb migrate copied every row into its owner's Graphiti group."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.db.copy_before_drop import CopyBeforeDrop, copy_marker
from nwb.db.database import PostgresDatabase, SqliteDatabase
from nwb.db.migrate import Migrator
from nwb.errors import MigrationError
from nwb.identity import Actor
from nwb.memory.guards import LengthGuard, QuoteGuard, SecretGuard
from nwb.memory.sql_copy import SqlMemoriesToGraphiti, SqlSessionNotesToGraphiti
from nwb.ports import Dialect, SqlDatabase
from tests.agent.scripted_memory import ScriptedPersonalMemory
from tests.conftest import MIGRATIONS, PG_DSN, PG_OK, ROOT, reset_pg

STEM = "0018_drop_sql_memory"
MEMORIES = (
    ("m-1", "P1", "active", "I work on the Atlas team", "2026-03-02 09:00:00"),
    ("m-2", "P2", "active", "My manager is Ravi", "2026-04-10 12:30:00"),
    ("m-3", "P1", "hidden", "Forget that I mentioned the offsite", "2026-05-01 08:00:00"),
)
NOTE = ("n-1", "s-1", "P1", "Asked about leave carry-over; prefers short answers", "2026-06-01 17:00:00")
SECRET = ("m-4", "P2", "active", "my password: hunter2hunter2", "2026-06-02 10:00:00")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SqlDatabase]:
    if request.param == "postgres":
        if not PG_OK:
            pytest.skip(f"Postgres not reachable at {PG_DSN}")
        reset_pg()
        d: SqlDatabase = PostgresDatabase(PG_DSN, max_size=2)
    else:
        d = SqliteDatabase(tmp_path / "t.sqlite")
    yield d
    d.close()


@pytest.fixture
def memory() -> ScriptedPersonalMemory:
    loader = ConfigLoader(ROOT / "config")
    graph, cfg = loader.load("graph", GraphConfig), loader.load("memory", MemoryConfig)
    guards = [LengthGuard(cfg.content_max_chars), SecretGuard(cfg.secret_patterns), QuoteGuard()]
    return ScriptedPersonalMemory(graph, cfg, guards)


def _before_0018(db: SqlDatabase, tmp_path: Path, memories: tuple[tuple[str, ...], ...]) -> None:
    pre = tmp_path / "pre"
    pre.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if int(path.name[:4]) < 18:
            shutil.copy(path, pre / path.name)
    Migrator(db, pre).migrate()
    with db.unit_of_work() as uow:
        uow.execute("INSERT INTO companies (id, name) VALUES ('NW', 'Northwind')")
        for person in ("P1", "P2"):
            uow.execute(
                "INSERT INTO people (id, company_id, name) VALUES (:id, 'NW', :name)", {"id": person, "name": person}
            )
        uow.execute("INSERT INTO sessions (id, person_id) VALUES ('s-1', 'P1')")
        for mid, person, status, content, at in memories:
            uow.execute(
                "INSERT INTO memories (id, person_id, kind, content, status, created_at) "
                "VALUES (:id, :p, 'fact', :c, :s, :at)",
                {"id": mid, "p": person, "c": content, "s": status, "at": datetime.fromisoformat(at)},
            )
        nid, session, person, summary, at = NOTE
        uow.execute(
            "INSERT INTO session_notes (id, session_id, person_id, summary, created_at) VALUES (:id, :s, :p, :t, :at)",
            {"id": nid, "s": session, "p": person, "t": summary, "at": datetime.fromisoformat(at)},
        )


def _left(db: SqlDatabase, table: str) -> int | None:
    with db.unit_of_work() as uow:
        if db.dialect is Dialect.POSTGRES:
            row = uow.fetchone("SELECT to_regclass(:t) IS NOT NULL AS ok", {"t": f"public.{table}"})
            if not (row and row["ok"]):
                return None
        elif uow.fetchone("SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = :t", {"t": table}) is None:
            return None
        count = uow.fetchone(f"SELECT count(*) AS n FROM {table}")
    return int(count["n"]) if count else 0


def _hook(db: SqlDatabase, memory: ScriptedPersonalMemory) -> CopyBeforeDrop:
    return CopyBeforeDrop(
        db, {STEM: (lambda: SqlMemoriesToGraphiti(lambda: memory), lambda: SqlSessionNotesToGraphiti(lambda: memory))}
    )


def _texts(memory: ScriptedPersonalMemory, person: str) -> dict[str, date | None]:
    facts = memory.facts_as_of(Actor("NW", person, date(2026, 12, 31)), include_history=True)
    return {f.text: f.valid_at.date() if f.valid_at else None for f in facts}


def test_drop_refuses_without_the_copy(db: SqlDatabase, tmp_path: Path) -> None:
    _before_0018(db, tmp_path, MEMORIES)
    with pytest.raises(MigrationError, match=STEM):
        Migrator(db, MIGRATIONS).migrate()
    assert (_left(db, "memories"), _left(db, "session_notes")) == (3, 1)


def test_memories_and_notes_survive_in_their_owners_groups(
    db: SqlDatabase, tmp_path: Path, memory: ScriptedPersonalMemory
) -> None:
    _before_0018(db, tmp_path, MEMORIES)
    Migrator(db, MIGRATIONS, before=_hook(db, memory)).migrate()
    assert (_left(db, "memories"), _left(db, "session_notes"), _left(db, "memory_versions")) == (None, None, None)
    with db.unit_of_work() as uow:
        names = {str(r["name"]) for r in uow.fetchall("SELECT name FROM schema_migrations")}
    assert {copy_marker("memories", 3), copy_marker("session_notes", 1)} <= names

    p1, p2 = _texts(memory, "P1"), _texts(memory, "P2")
    assert p1["I work on the Atlas team"] == date(2026, 3, 2)
    assert p1[NOTE[3]] == date(2026, 6, 1)
    assert p2 == {"My manager is Ravi": date(2026, 4, 10)}  # each row lands only in its owner's group
    assert all("offsite" not in text for text in p1)  # hidden by its owner: not carried into recall


def test_a_rejected_row_blocks_the_drop_and_a_rerun_resumes(
    db: SqlDatabase, tmp_path: Path, memory: ScriptedPersonalMemory
) -> None:
    _before_0018(db, tmp_path, (*MEMORIES, SECRET))
    with pytest.raises(MigrationError, match="m-4"):
        Migrator(db, MIGRATIONS, before=_hook(db, memory)).migrate()
    assert (_left(db, "memories"), _left(db, "session_notes")) == (4, 1)
    assert "My manager is Ravi" in _texts(memory, "P2")

    with db.unit_of_work() as uow:  # the operator removes the secret by hand, then migrates again
        uow.execute("DELETE FROM memories WHERE id = 'm-4'")
    Migrator(db, MIGRATIONS, before=_hook(db, memory)).migrate()
    assert _left(db, "memories") is None
    p2 = memory.facts_as_of(Actor("NW", "P2", date(2026, 12, 31)), include_history=True)
    assert [f.text for f in p2] == ["My manager is Ravi"]  # written once, not again on the rerun
