"""Replay the GraphRAG-SDK and Graphiti spike with no network and no API key.

``CallRecorder.key`` is the first 32 hex chars of SHA-256 over
``{"model", "kind", "payload"}`` (JSON, sorted keys). UUID strings in that
blob are replaced with ``<uuid>`` before hashing, because both libraries mint
a fresh id per run. The cassette filename is that key. Replay mode raises
``RecordingMissError`` on a miss, so a hit never opens a socket.

The replayable cassette set is the one whose GraphRAG model is
``{provider}/{bare model}`` and whose Graphiti model is that bare model, with
extraction prompts (JSON) as well as Graphiti calls. Completion cites
``Document.path``. The spike ingests tables by basename so that path is not a
machine-specific temp directory; ``d6989050….json`` is the completion for that
stable path, with the answer copied from the earlier messages cassette (the
old key had an absolute temp path baked into the prompt).
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import json
import socket
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from nwb.errors import ConfigError
from nwb.llm.settings import LLMSettings, ModelRef

pytest.importorskip(
    "graphrag_sdk",
    reason="graph extra is not installed; pip install -e '.[graph]'",
)
pytest.importorskip(
    "graphiti_core",
    reason="graph extra is not installed; pip install -e '.[graph]'",
)

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.kg.models import GraphModels
from nwb.kg.recording import CallRecorder, RecordMode
from nwb.kg.tokenizer import TokenizerCache
from nwb.llm.factory import LLMFactory
from nwb.memory.embeddings import build_embedder

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "fixtures" / "llm_calls" / "spike"
_LOCAL = {"127.0.0.1", "localhost", "::1"}


def _spike() -> Any:
    path = ROOT / "scripts" / "spike_graph_libs.py"
    spec = importlib.util.spec_from_file_location("spike_graph_libs", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _docs() -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(CASSETTES.glob("*.json"))]


def _replay_ref(docs: list[dict[str, Any]]) -> ModelRef:
    """Pick the cassette model the spike can replay as one ``provider:model`` ref."""
    prompts: Counter[str] = Counter()
    graphiti: Counter[str] = Counter()
    for doc in docs:
        response = doc["response"]
        text = response if isinstance(response, str) else json.dumps(response)
        if doc["kind"] == "graphiti":
            graphiti[doc["model"]] += 1
        elif doc["kind"] == "prompt" and ("```json" in text[:20] or text.lstrip()[:1] in "{["):
            prompts[doc["model"]] += 1
    best: tuple[int, ModelRef] | None = None
    for model, n_prompt in prompts.items():
        provider, sep, bare = model.partition("/")
        if not sep or bare not in graphiti:
            continue
        score = n_prompt + graphiti[bare]
        if best is None or score > best[0]:
            best = (score, ModelRef(provider, bare))
    if best is None:
        raise AssertionError(f"no replayable cassette set in {CASSETTES}")
    return best[1]


def _expected_members(people_csv: str, teams_csv: str) -> set[tuple[str, str]]:
    teams = {row["team_id"]: row["team_name"] for row in csv.DictReader(io.StringIO(teams_csv))}
    return {(row["full_name"], teams[row["team_id"]]) for row in csv.DictReader(io.StringIO(people_csv))}


def test_recorder_key_masks_uuids_and_matches_filenames() -> None:
    """The key ignores freshly minted UUIDs and is the cassette filename."""
    recorder = CallRecorder(CASSETTES)
    left = "11111111-2222-3333-4444-555555555555"
    right = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert recorder.key("m", "graphiti", {"id": left}) == recorder.key("m", "graphiti", {"id": right})
    assert recorder.key("m", "graphiti", {"id": left}) != recorder.key("m", "graphiti", {"id": "not-a-uuid"})
    names = {path.stem for path in CASSETTES.glob("*.json")}
    assert names
    assert all(len(name) == 32 for name in names)


def test_spike_replays_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh local graph replays the spike from cassettes. No API key, and no network once the guard is on.

    ``tokenizer.prepare`` runs first and fills the cache from ``config/graph.yaml``. The socket guard
    is installed after that, so GraphRAG's chunker reads the local encoding files.
    """
    for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    root_conf = ConfigLoader(ROOT / "config")
    graph_cfg = root_conf.load("graph", GraphConfig)
    tokenizer = TokenizerCache.from_config(ROOT, graph_cfg)
    tokenizer.prepare()
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tokenizer.directory()))
    original = socket.socket.connect

    def _local_only(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in _LOCAL:
            raise AssertionError(f"replay opened a network connection to {address!r}")
        original(self, address)

    monkeypatch.setattr(socket.socket, "connect", _local_only)

    docs = _docs()
    ref = _replay_ref(docs)
    settings = LLMSettings(_env_file=None, llm_graph=(ref,))
    models = GraphModels(LLMFactory(settings), graph_cfg, ROOT)
    recorder = CallRecorder(CASSETTES, RecordMode.REPLAY)
    embedder = build_embedder(root_conf.load("memory", MemoryConfig).embedder)
    try:
        embedder.embed(["offline probe"])
    except Exception as exc:
        pytest.skip(f"embedding model is not available offline: {exc}")

    spike = _spike()
    try:
        result = asyncio.run(
            spike.run(
                recorder,
                models.sdk_llm(recorder),
                models.graphiti_llm(recorder),
                embedder,
                tmp_path / "falkor",
                None,
                tokenizer,
            )
        )
    except ConfigError as exc:
        pytest.skip(f"no local FalkorDB for the spike replay: {exc}")

    assert recorder.misses == 0
    assert recorder.hits > 0
    completions = [
        doc["response"] for doc in docs if doc["kind"] == "messages" and doc["model"] == f"{ref.provider}/{ref.model}"
    ]
    assert completions
    assert result.answer == completions[0]
    assert {tuple(row) for row in result.member_of} == _expected_members(spike.PEOPLE, spike.TEAMS)
    assert all(table["records"] > 0 for table in result.tables)
    cassette_text = "\n".join(json.dumps(doc) for doc in docs)
    assert result.personal_facts
    assert all(fact["fact"] in cassette_text for fact in result.personal_facts)
