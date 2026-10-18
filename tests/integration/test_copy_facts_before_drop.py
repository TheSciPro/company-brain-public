"""0019 drops the SQL facts table only after nwb migrate copied every row into the per-level graphs."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from nwb.db.copy_before_drop import CopyBeforeDrop, copy_marker
from nwb.db.database import PostgresDatabase, SqliteDatabase
from nwb.db.migrate import Migrator
from nwb.errors import MigrationError
from nwb.ports import Dialect, SqlDatabase
from nwb.temporal.fact_copy import SqlFactsToGraph
from nwb.temporal.facts import Authority, Fact
from nwb.temporal.graph_facts import GraphFactBook
from nwb.temporal.precedence import PrecedenceEngine
from tests.conftest import MIGRATIONS, PG_DSN, PG_OK, reset_pg

LEVELS = ("public", "internal", "confidential", "highly_confidential")
FALLBACK = "highly_confidential"


def _fact(value: str, start: str, src: str, **extra: object) -> Fact:
    base = Fact(
        key="leave_days",
        about="NW",
        value=value,
        valid_from=date.fromisoformat(start),
        authority=Authority(3),
        src_type="record" if src.startswith("rec") else "document",
        src_id=src,
        quote=f"quote {value}",
        company_id="NW",
        unit="days",
        label="annual leave",
    )
    return base.with_(**extra)


OLD = _fact("20", "2026-01-01", "doc-int", status="superseded", valid_to=date(2026, 3, 31))
NEW = _fact("22", "2026-04-01", "doc-int", supersedes_id=OLD.id)
SCOPED = _fact("25", "2026-02-01", "rec-conf", scope="client:ACC-1", overrides_id=OLD.id)
ORPHAN = _fact("30", "2026-02-01", "doc-gone", key="carry_over_days")
FACTS = (OLD, NEW, SCOPED, ORPHAN)


def _open(backend: str, tmp_path: Path) -> SqlDatabase:
    if backend == "postgres":
        if not PG_OK:
            pytest.skip(f"Postgres not reachable at {PG_DSN}")
        reset_pg()
        return PostgresDatabase(PG_DSN, max_size=2)
    return SqliteDatabase(tmp_path / "t.sqlite")


def _before_0019(db: SqlDatabase, tmp_path: Path) -> None:
    pre = tmp_path / "pre"
    pre.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if int(path.name[:4]) < 19:
            shutil.copy(path, pre / path.name)
    Migrator(db, pre).migrate()
    with db.unit_of_work() as uow:
        uow.execute("INSERT INTO companies (id, name) VALUES ('NW', 'Northwind')")
        uow.execute(
            "INSERT INTO documents (id, company_id, title, classification, blob_url, hash) "
            "VALUES ('doc-int', 'NW', 'Leave policy', 'internal', 'blob://int', 'h1')"
        )
        uow.execute(
            "INSERT INTO records (id, company_id, source, source_id, record_type, classification, share_scope, "
            "payload, hash) VALUES ('rec-conf', 'NW', 't', 'c', 'contract', 'confidential', 'team', '{}', 'r1')"
        )
        for f in FACTS:
            uow.execute(
                "INSERT INTO facts (id, company_id, key, about, scope, value, unit, valid_from, valid_to, authority, "
                "status, src_type, src_id, quote, supersedes_id, overrides_id, related_id, label) VALUES "
                "(:id, :company_id, :key, :about, :scope, :value, :unit, :valid_from, :valid_to, :authority, "
                ":status, :src_type, :src_id, :quote, :supersedes_id, :overrides_id, :related_id, :label)",
                {
                    "id": f.id,
                    "company_id": f.company_id,
                    "key": f.key,
                    "about": f.about,
                    "scope": f.scope,
                    "value": f.value,
                    "unit": f.unit,
                    "valid_from": f.valid_from,
                    "valid_to": f.valid_to,
                    "authority": int(f.authority),
                    "status": f.status,
                    "src_type": f.src_type,
                    "src_id": f.src_id,
                    "quote": f.quote,
                    "supersedes_id": f.supersedes_id,
                    "overrides_id": f.overrides_id,
                    "related_id": f.related_id,
                    "label": f.label,
                },
            )


def _facts_left(db: SqlDatabase) -> int | None:
    with db.unit_of_work() as uow:
        if db.dialect is Dialect.POSTGRES:
            row = uow.fetchone("SELECT to_regclass('public.facts') IS NOT NULL AS ok")
            if not (row and row["ok"]):
                return None
        elif uow.fetchone("SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = 'facts'") is None:
            return None
        count = uow.fetchone("SELECT count(*) AS n FROM facts")
    return int(count["n"]) if count else 0


class _Short:
    table = "facts"

    def copy(self, db: SqlDatabase) -> int:
        del db
        return len(FACTS) - 1


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_drop_refuses_without_the_copy_and_after_a_short_copy(backend: str, tmp_path: Path) -> None:
    db = _open(backend, tmp_path)
    try:
        _before_0019(db, tmp_path)
        with pytest.raises(MigrationError, match="0019_drop_facts"):
            Migrator(db, MIGRATIONS).migrate()
        assert _facts_left(db) == len(FACTS)

        short = CopyBeforeDrop(db, {"0019_drop_facts": (_Short,)})
        with pytest.raises(MigrationError, match="copied 3 of 4 facts rows"):
            Migrator(db, MIGRATIONS, before=short).migrate()
        assert _facts_left(db) == len(FACTS)
    finally:
        db.close()


def test_facts_survive_the_drop_in_their_level_graphs(backend: str, tmp_path: Path) -> None:
    db = _open(backend, tmp_path)
    book = GraphFactBook.open_lite(tmp_path / "facts.falkor", company="NW", levels=LEVELS)
    try:
        _before_0019(db, tmp_path)
        copies = CopyBeforeDrop(db, {"0019_drop_facts": (lambda: SqlFactsToGraph(lambda: book, FALLBACK),)})
        ran = Migrator(db, MIGRATIONS, before=copies).migrate()
        assert any(name.startswith("0019_drop_facts") for name in ran)
        assert _facts_left(db) is None
        with db.unit_of_work() as uow:
            marker = uow.fetchone("SELECT name FROM schema_migrations WHERE name = :n", {"n": copy_marker("facts", 4)})
        assert marker is not None

        old, new, scoped, orphan = (book.get(f.id) for f in FACTS)
        assert old is not None and new is not None and scoped is not None and orphan is not None
        assert (old.level, new.level, scoped.level, orphan.level) == ("internal", "internal", "confidential", FALLBACK)
        assert (old.status, old.valid_to) == ("superseded", date(2026, 3, 31))
        assert (new.status, new.valid_to, new.supersedes_id) == ("current", None, OLD.id)
        assert (scoped.scope, scoped.overrides_id, scoped.authority) == ("client:ACC-1", OLD.id, Authority(3))
        assert (new.unit, new.label, new.title, new.quote) == ("days", "annual leave", "Leave policy", "quote 22")

        engine = PrecedenceEngine(book)
        earlier = engine.as_of("leave_days", "NW", date(2026, 2, 1))
        later = engine.as_of("leave_days", "NW", date(2026, 6, 1))
        assert earlier.fact is not None and earlier.fact.value == "20"
        assert later.fact is not None and later.fact.value == "22"
        assert later.replaced is not None and later.replaced.value == "20"
    finally:
        book.release()
        db.close()


def test_an_empty_facts_table_drops_without_opening_the_graphs(backend: str, tmp_path: Path) -> None:
    db = _open(backend, tmp_path)
    try:
        opened: list[str] = []

        def never() -> GraphFactBook:
            opened.append("graph")
            raise AssertionError("an empty table needs no graph")

        copies = CopyBeforeDrop(db, {"0019_drop_facts": (lambda: SqlFactsToGraph(never, FALLBACK),)})
        Migrator(db, MIGRATIONS, before=copies).migrate()
        assert _facts_left(db) is None and opened == []
    finally:
        db.close()
