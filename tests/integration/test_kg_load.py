"""Local FalkorDB load: fresh graph names, non-zero counts, replayed prose extraction.

The prose path replays cassettes produced by the offline stand-in. It does not call a hosted model.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")

from graphrag_sdk import Ontology

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, ProseDecl
from nwb.ingest.acl import classification_config
from nwb.kg.catalog import materialise
from nwb.kg.load import load_company
from nwb.kg.offline_llm import OfflineSdkLLM
from nwb.kg.ontology import discover_from_sources
from nwb.kg.recording import CallRecorder, RecordedSdkLLM, RecordMode
from nwb.kg.server import falkor_endpoint
from nwb.memory.embeddings import HashingEmbedder

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "fixtures" / "llm_calls" / "kg_load"
PROSE = ROOT / "tests" / "fixtures" / "kg_prose"
_LOCAL = {"127.0.0.1", "localhost", "::1"}
_ORIGINS = {"EXTRACTED", "INFERRED"}


def _local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    original = socket.socket.connect

    def _guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in _LOCAL:
            raise AssertionError(f"load opened a network connection to {address!r}")
        original(self, address)

    monkeypatch.setattr(socket.socket, "connect", _guard)


def _loader_script() -> Any:
    path = ROOT / "scripts" / "load_company_graph.py"
    spec = importlib.util.spec_from_file_location("load_company_graph_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prefix_flag_defaults_to_the_runtime_setting() -> None:
    """Omitted --prefix is unset so the loader reads GRAPH_PREFIX. Empty means no prefix."""
    module = _loader_script()
    omitted = module._parser().parse_args([])
    blank = module._parser().parse_args(["--prefix", ""])
    assert omitted.prefix is None
    assert blank.prefix == ""
    assert module.graph_name_prefix(omitted.prefix, "shared") == "shared"
    assert module.graph_name_prefix(blank.prefix, "shared") == ""


def test_live_script_names_missing_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("FALKORDB_URL", raising=False)
    env = os.environ.copy()
    env.pop("OPENROUTER_API_KEY", None)
    env.pop("FALKORDB_URL", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_graph_live.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "OPENROUTER_API_KEY" in proc.stdout
    assert "FALKORDB_URL" in proc.stdout


def test_tables_load_nonzero_on_a_fresh_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real configured tables, capped rows, hashing embedder, offline LLM. Counts come from the graph."""
    _local_only(monkeypatch)
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    prefix = "it_"
    with falkor_endpoint(None, tmp_path / "falkor") as endpoint:
        report = asyncio.run(
            load_company(
                cfg,
                data_dir=ROOT / "data",
                company="acme",
                endpoint=endpoint,
                llm=OfflineSdkLLM("offline-kg"),
                embedder=HashingEmbedder(),
                work_dir=tmp_path / "work",
                prefix=prefix,
                max_rows=20,
                tables_only=True,
                llm_label="offline",
                ontology_path=ROOT / cfg.company.ontology.file,
            )
        )
    assert report.levels, "expected at least one access level"
    for level in report.levels:
        assert level.graph == f"{prefix}acme_kb_{level.level}"
        assert level.entities > 0, level
        assert level.edges > 0, level
        assert set(level.origins) <= _ORIGINS
        assert level.origins.get("EXTRACTED", 0) > 0


def _prose_cfg() -> GraphConfig:
    """The fixture directory is not the company export, so this load has no tables and one prose root."""
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    assert cfg.company.load is not None
    load = cfg.company.load.model_copy(update={"tables": (), "prose": (ProseDecl(root="."),)})
    return cfg.model_copy(update={"company": cfg.company.model_copy(update={"load": load})})


async def _prose_load(cfg: GraphConfig, llm: Any, endpoint: Any, work_dir: Path) -> Any:
    """Discover the way the recorded load did, then ingest under that ontology only.

    Cassettes were recorded with ``Ontology.from_sources`` on relative document ids
    after changing into the level directory, and with an empty existing ontology
    (the fixture declares no tables). The returned object is passed through; a
    JSON round-trip would change the extraction prompts.
    """
    load = cfg.company.load
    assert load is not None
    levels = tuple(level.code for level in classification_config().levels)
    bundles = materialise(PROSE, load, work_dir, levels)
    draft = Ontology()
    previous = Path.cwd()
    try:
        for bundle in bundles:
            if not bundle.prose:
                continue
            os.chdir(bundle.directory)
            draft = await discover_from_sources(
                [doc.document_id for doc in bundle.prose],
                llm,
                method=load.discover_method,
                boundaries=load.boundaries,
                sample_chunks_per_doc=load.sample_chunks_per_doc,
                concurrency=cfg.company.max_concurrency,
                seed=load.seed,
                existing=Ontology(),
            )
    finally:
        os.chdir(previous)
    return await load_company(
        cfg,
        data_dir=PROSE,
        company="acme",
        endpoint=endpoint,
        llm=llm,
        embedder=HashingEmbedder(),
        work_dir=work_dir,
        prefix="prose_",
        tables_only=False,
        llm_label="offline",
        ontology=draft,
    )


def test_prose_replays_from_cassettes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture notes replay through the offline stand-in's recordings. A miss is a test failure.

    Discovery runs first and its ontology is what the load is allowed to use. The load
    does not discover again.
    """
    _local_only(monkeypatch)
    assert list(CASSETTES.glob("*.json")), "record prose cassettes into tests/fixtures/llm_calls/kg_load"
    cfg = _prose_cfg()
    recorder = CallRecorder(CASSETTES, RecordMode.REPLAY)
    model = cfg.company.load.offline_model if cfg.company.load else "offline-kg"
    llm = RecordedSdkLLM(OfflineSdkLLM(model), recorder)
    with falkor_endpoint(None, tmp_path / "falkor") as endpoint:
        report = asyncio.run(_prose_load(cfg, llm, endpoint, tmp_path / "work"))
    assert recorder.misses == 0
    assert recorder.hits > 0
    assert report.levels
    assert all(level.entities > 0 for level in report.levels)
    assert all(set(level.origins) <= _ORIGINS for level in report.levels)
