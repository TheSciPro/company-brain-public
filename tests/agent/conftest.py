"""Agent-runtime fixtures: a loaded SQLite company, a Container wired with fakes (no network, no graph)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nwb.agent.clock import FixedClock
from nwb.agent.context import RuntimeContext
from nwb.agent.graph import Models
from nwb.conf import MemoryConfig
from nwb.config import Settings
from nwb.container import Container
from nwb.db.migrate import Migrator
from nwb.identity import Actor
from tests.agent.scripted_memory import ScriptedPersonalMemory
from tests.conftest import DATA, MIGRATIONS, ROOT
from tests.fakes import ScriptedChatModel

ASOF = date(2026, 9, 28)


def point_company_graphs(c: Container, directory: Path) -> None:
    """Aim this container's company graphs at ``directory``.

    Facts and search still resolve through ``graph.yaml`` ``server.local_dir``;
    the test substitutes that directory so runs do not share ``var/falkordb``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    server = c.graph_config.server.model_copy(update={"local_dir": str(directory)})
    c.graph_config = c.graph_config.model_copy(update={"server": server})


@pytest.fixture(scope="session")
def company_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate + load layer 1/2 once per test session; tests get a copy."""
    path = tmp_path_factory.mktemp("tpl") / "company.sqlite"
    c = Container.from_settings(test_settings(path, path.parent / "blobs"))
    point_company_graphs(c, path.parent / "company-graphs")
    Migrator(c.db, MIGRATIONS).migrate()
    c.layer1_loader().load()
    c.layer2_loader().load()
    c.close()
    return path


EXTRACTIONS = ROOT / "tests" / "fixtures" / "extractions"


def test_settings(db_path: Path, blob_dir: Path, **over: object) -> Settings:
    """Hermetic settings: SQLite, recorded extractions only (no LLM at ingest), in-memory agent persistence."""
    base: dict[str, object] = {
        "sqlite_path": db_path,
        "data_dir": DATA,
        "blob_dir": blob_dir,
        "config_dir": ROOT / "config",
        "prompts_dir": ROOT / "prompts",
        "agent_dir": ROOT / "agent",
        "personas_file": ROOT / "demo" / "personas.yaml",
        "extraction_mode": "cache_only",
        "extraction_cache_dir": EXTRACTIONS,
        "checkpointer": "memory",
        "falkor_lite_path": db_path.with_name(db_path.stem + ".falkor"),
    }
    return Settings(**{**base, **over})  # type: ignore[arg-type]


def hashing_memory_config(c: Container) -> MemoryConfig:
    """Real memory.yaml with the hashing embedder (the scripted store does not call it)."""
    mc = c.memory_config
    return mc.model_copy(update={"embedder": mc.embedder.model_copy(update={"kind": "hashing", "dim": 256})})


class Harness:
    def __init__(self, c: Container, model: ScriptedChatModel, memory: ScriptedPersonalMemory) -> None:
        self.c = c
        self.model = model
        self.memory = memory

    def actor(self, person: str = "NW1103", session: str | None = None, message: str | None = None) -> Actor:
        return Actor(self.c.settings.company_id, person, ASOF, session_id=session, message_id=message)

    def ctx(self, person: str = "NW1103", session: str = "s-test", run: str = "run-test") -> RuntimeContext:
        return RuntimeContext(self.c.settings.company_id, person, session, run, ASOF.isoformat(), "")


@pytest.fixture
def harness(company_template: Path, tmp_path: Path) -> Iterator[Harness]:
    db_path = tmp_path / "nwb.sqlite"
    shutil.copy(company_template, db_path)
    falkor_src = company_template.with_name(company_template.stem + ".falkor")
    if falkor_src.exists():
        shutil.copy(falkor_src, db_path.with_name(db_path.stem + ".falkor"))
    graph_src = company_template.parent / "company-graphs"
    graph_dest = tmp_path / "company-graphs"
    if graph_src.exists():
        shutil.copytree(graph_src, graph_dest)
    c = Container.from_settings(test_settings(db_path, company_template.parent / "blobs"))
    point_company_graphs(c, graph_dest)
    c.memory_config = hashing_memory_config(c)
    c.clock = FixedClock(datetime(2026, 9, 28, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    memory = ScriptedPersonalMemory(c.graph_config, c.memory_config, c.write_guards())
    c.personal_memory = memory  # before any cached property builds Graphiti
    model = ScriptedChatModel()
    h = Harness(c, model, memory)
    # The real chain needs provider keys and would call out. Tests drive this script instead.
    c.models = Models(model, model)
    c.llm.chat = lambda ref, role=None: model  # type: ignore[method-assign]
    c._sync_graph = lambda: None  # type: ignore[method-assign]
    yield h
    c.close()
