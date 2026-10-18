"""Swap test (audit Part E.4): rename a client in the data, re-ingest, and every answer follows. No code edits.

The recorded LLM extractions are data too: the same rename is applied to them and they are re-keyed by the new
document hash, exactly as a fresh extraction of the renamed document would be cached.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from nwb.config import Settings
from nwb.container import Container
from nwb.db.migrate import Migrator
from nwb.identity import Actor
from tests.agent.conftest import point_company_graphs
from tests.conftest import DATA, MIGRATIONS, ROOT

OLD, NEW = "Brightmart", "Quillfeather"
FIXTURES = ROOT / "tests" / "fixtures" / "extractions"


def _renamed_copy(tmp: Path) -> tuple[Path, Path]:
    from nwb.knowledge.extract import ExtractionCache

    data, cache = tmp / "data", tmp / "extractions"
    shutil.copytree(DATA, data)
    cache.mkdir()
    old_keys: dict[str, str] = {}
    for p in data.rglob("*"):
        if p.is_file() and p.suffix in {".md", ".json", ".jsonl", ".csv"}:
            text = p.read_text()
            if p.suffix == ".md":
                old_keys[str(p)] = ExtractionCache.key(text)
            if OLD in text:
                p.write_text(text.replace(OLD, NEW))
    for path, old in old_keys.items():
        src = FIXTURES / f"{old}.json"
        if src.exists():
            new = ExtractionCache.key(Path(path).read_text())
            (cache / f"{new}.json").write_text(src.read_text().replace(OLD, NEW))
    return data, cache


def test_renamed_client_answers_follow(tmp_path: Path) -> None:
    data, cache = _renamed_copy(tmp_path)
    s = Settings(
        sqlite_path=tmp_path / "x.sqlite",
        data_dir=data,
        blob_dir=tmp_path / "blobs",
        extraction_mode="cache_only",
        extraction_cache_dir=cache,
        falkor_lite_path=tmp_path / "x.falkor",
    )
    c = Container.from_settings(s)
    point_company_graphs(c, tmp_path / "company-graphs")
    try:
        Migrator(c.db, MIGRATIONS).migrate()
        c.layer1_loader().load()
        c.layer2_loader().load()
        actor = Actor(s.company_id, "NW0011", date(2026, 9, 28))
        res = c.knowledge.lookup(actor, "What discount did we agree?", client=NEW)
        assert res.best is not None and res.best.answer is not None
        assert res.best.answer.value == "18"
        assert c.knowledge.clients.resolve(OLD) is None
    finally:
        c.close()
