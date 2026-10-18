from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

# Before nwb is imported: Settings reads env_file at class definition time.
os.environ["NWB_DISABLE_DOTENV"] = "1"

import psycopg
import pytest

from nwb.conf import ConfigLoader, SourcesConfig
from nwb.db.database import PostgresDatabase, SqliteDatabase
from nwb.db.migrate import Migrator
from nwb.knowledge.extract import CachedExtractor, ExtractionCache, FactExtractor
from nwb.ports import SqlDatabase

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
DATA = ROOT / "data"

# Hermetic tests: a developer's shell or .env may point these at real state (a dev DB, cloud FalkorDB, another LLM chain).
# NWB_DISABLE_DOTENV stops Settings from reading .env. Popping the variables stops the shell from winning.
for _var in (
    "DATABASE_URL",
    "NWB_DATABASE_URL",
    "NWB_PG_DSN",
    "NWB_SQLITE_PATH",
    "NWB_DB_BACKEND",
    "MIGRATE_URL",
    "NWB_MIGRATE_URL",
    "NEON_DIRECT_URL",
    "FALKORDB_URL",
    "NWB_FALKOR_LITE_PATH",
    "NWB_BLOB_DIR",
    "NWB_CHECKPOINTER",
    "NWB_CHECKPOINT_PATH",
    "LLM_MAIN",
    "LLM_BACKGROUND",
    "LLM_GRAPH",
):
    os.environ.pop(_var, None)
# Empty sentinels: an explicit env value beats a dotenv file if a test opts back into one.
# Importing litellm runs load_dotenv(), which never overwrites a set variable, so these also stop .env leaking in.
for _var in ("DATABASE_URL", "FALKORDB_URL", "MIGRATE_URL", "NWB_MIGRATE_URL", "NEON_DIRECT_URL"):
    os.environ[_var] = ""
# Live tests call a hosted model with a real key. A key in the shell or .env is not consent: they run only
# with NWB_LIVE=1. ``pytest_collection_modifyitems`` skips every item marked ``live`` otherwise.
LIVE = os.environ.get("NWB_LIVE") == "1"
PG_DSN = os.environ.get("NWB_TEST_PG_DSN", "postgresql://postgres@localhost:5433/nwb_test")
# Local fixture password for the nw_app login. Not a deployed secret; never log it.
APP_ROLE_PASSWORD = "nwb-test-app-role"


def _pg_available() -> bool:
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


PG_OK = _pg_available()


def reset_pg() -> None:
    with psycopg.connect(PG_DSN, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
        # nw_app is cluster-wide (shared with the dev db): keep the role, privileges die with the schema


def app_dsn() -> str:
    """DSN that logs in as nw_app against the same database as ``PG_DSN``."""
    parsed = urlparse(PG_DSN)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    path = parsed.path or "/nwb_test"
    netloc = f"{quote('nw_app')}:{quote(APP_ROLE_PASSWORD)}@{host}:{port}"
    return urlunparse((parsed.scheme or "postgresql", netloc, path, "", parsed.query, ""))


def fresh_db(backend: str, tmp_path: Path) -> SqlDatabase:
    db: SqlDatabase
    if backend == "postgres":
        if not PG_OK:
            pytest.skip(f"Postgres not reachable at {PG_DSN}")
        reset_pg()
        db = PostgresDatabase(PG_DSN, max_size=2)
    else:
        db = SqliteDatabase(tmp_path / "t.sqlite")
    Migrator(db, MIGRATIONS).migrate()
    return db


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SqlDatabase]:
    d = fresh_db(request.param, tmp_path)
    yield d
    d.close()


def shipped_sources() -> SourcesConfig:
    """The shipped connector config (config/sources.yaml), as the container loads it."""
    return ConfigLoader(ROOT / "config").load("sources", SourcesConfig)


def cached_extractor() -> FactExtractor:
    """Replay-only fact extraction from the recorded fixtures: tests never call a model."""
    return CachedExtractor(ExtractionCache(ROOT / "tests" / "fixtures" / "extractions"), None)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``live`` tests unless NWB_LIVE=1. ``-m live`` alone selects them but still needs the flag."""
    if LIVE:
        return
    skip = pytest.mark.skip(reason="live test: set NWB_LIVE=1 to call a hosted model")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
