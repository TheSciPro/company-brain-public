"""Spike (risk 1): prove GraphRAG-SDK and Graphiti work against a FalkorDB reachable from the box, on free models.

    LLM_GRAPH=groq:qwen/qwen3.8-27b PYTHONPATH=src:. .venv/bin/python scripts/spike_graph_libs.py [--mode record]

Uses a tiny synthetic company (not the demo data) so it proves the libraries, not our data. Every LLM call is
recorded under tests/fixtures/llm_calls/spike/; ``tests/integration/test_graph_spike.py`` replays it offline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.nodes import EpisodeType
from graphrag_sdk import Column, ConnectionConfig, Entity, GraphRAG, Link, Ontology, TableMapping
from graphrag_sdk.ingestion.extraction_strategies.entity_extractors import LLMExtractor
from graphrag_sdk.ingestion.extraction_strategies.graph_extraction import GraphExtraction

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.kg.embedding import EmbeddingReranker, GraphitiEmbedder, SdkEmbedder
from nwb.kg.models import GraphModels
from nwb.kg.recording import CallRecorder, RecordMode
from nwb.kg.server import FalkorEndpoint, falkor_endpoint
from nwb.kg.tokenizer import TokenizerCache
from nwb.llm.factory import LLMFactory
from nwb.llm.settings import LLMSettings
from nwb.memory.embeddings import Embedder, build_embedder
from nwb.obs.log import get_logger

log = get_logger(__name__)

TEAMS = "team_id,team_name,region\nT1,Payments,EU\nT2,Platform,US\n"
PEOPLE = "emp_id,full_name,title,team_id\nE1,Asha Rao,Engineer,T1\nE2,Ben Ode,Manager,T1\nE3,Cy Lin,SRE,T2\n"
POLICY = (
    "Payments team on-call policy. On-call engineers may claim one comp day after a weekend incident. "
    "Ben Ode approves comp days for the Payments team."
)
EPISODES = (
    ("s1", "user: I prefer async standups over video calls. I work on the Payments team.", 0),
    ("s2", "user: Update: I moved to the Platform team this week.", 10),
)


@dataclass
class SpikeResult:
    tables: list[dict[str, Any]] = field(default_factory=list)
    prose_entities: int = 0
    finalize: dict[str, int] = field(default_factory=dict)
    member_of: list[list[Any]] = field(default_factory=list)
    retrieved: int = 0
    answer: str = ""
    abstain_retrieved: int = 0
    personal_facts: list[dict[str, Any]] = field(default_factory=list)


async def run_sdk(ep: FalkorEndpoint, llm: Any, embedder: Embedder, workdir: Path, res: SpikeResult) -> None:
    (workdir / "teams.csv").write_text(TEAMS)
    (workdir / "people.csv").write_text(PEOPLE)
    # Retrieval cites Document.path, and that string is inside the completion
    # prompt, which is inside the recording key. Ingest by basename so the key
    # does not contain a machine-specific temp directory.
    previous = Path.cwd()
    os.chdir(workdir)
    try:
        await _run_sdk_loaded(ep, llm, embedder, res)
    finally:
        os.chdir(previous)


async def _run_sdk_loaded(ep: FalkorEndpoint, llm: Any, embedder: Embedder, res: SpikeResult) -> None:
    ontology = Ontology(
        entities=[Entity(label="Person"), Entity(label="Team"), Entity(label="Policy")],
        tables=[
            TableMapping(
                source="teams.csv",
                label="Team",
                key="team_id",
                name="team_name",
                properties={"region": Column("region")},
            ),
            TableMapping(
                source="people.csv",
                label="Person",
                key="emp_id",
                name="full_name",
                properties={"title": Column("title")},
                links=[Link("MEMBER_OF", to="Team", by="team_id")],
            ),
        ],
    )
    conn = ConnectionConfig(
        host=ep.host, port=ep.port, username=ep.username, password=ep.password, ssl=ep.ssl, graph_name="spike_kb"
    )
    async with GraphRAG(
        connection=conn, llm=llm, embedder=SdkEmbedder(embedder), embedding_dimension=embedder.dim, ontology=ontology
    ) as rag:
        for name in ("teams.csv", "people.csv"):
            r = await rag.ingest(name)
            res.tables.append({"source": name, "records": r.records, "entities": r.entities, "edges": r.edges})
        extractor = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm))
        prose = await rag.ingest(text=POLICY, document_id="oncall-policy.md", extractor=extractor)
        res.prose_entities = prose.nodes_created
        fin = await rag.finalize()
        res.finalize = {
            "deduplicated": fin.entities_deduplicated,
            "probable_duplicates": len(fin.probable_duplicates),
            "property_conflicts": len(fin.property_conflicts),
        }
        res.member_of = await rag.query(
            "MATCH (p:Person)-[r:RELATES {rel_type:'MEMBER_OF'}]->(t:Team) RETURN p.name, t.name ORDER BY p.name"
        )
        got = await rag.retrieve("Who approves comp days for the Payments team?")
        res.retrieved = len(got.items)
        ans = await rag.completion("Who approves comp days for the Payments team?")
        res.answer = ans.answer
        none = await rag.retrieve("What is the parental leave policy in Brazil?")
        res.abstain_retrieved = sum(1 for i in none.items if "Source Document Passages" in i.content)


async def run_graphiti(ep: FalkorEndpoint, llm: Any, embedder: Embedder, res: SpikeResult) -> None:
    group = "spike_acme__u1"
    g = Graphiti(
        graph_driver=FalkorDriver(
            host=ep.host, port=ep.port, username=ep.username, password=ep.password, database=group
        ),
        llm_client=llm,
        embedder=GraphitiEmbedder(embedder),
        cross_encoder=EmbeddingReranker(embedder),
    )
    try:
        await g.build_indices_and_constraints()
        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        for name, body, day in EPISODES:
            await g.add_episode(
                name=name,
                episode_body=body,
                source=EpisodeType.message,
                source_description="chat",
                reference_time=t0 + timedelta(days=day),
                group_id=group,
            )
        for e in await g.search("Which team does the user work on?", group_ids=[group], num_results=10):
            res.personal_facts.append({"fact": e.fact, "valid_at": e.valid_at, "invalid_at": e.invalid_at})
    finally:
        await g.close()


async def run(
    recorder: CallRecorder,
    sdk_llm: Any,
    graphiti_llm: Any,
    embedder: Embedder,
    data_dir: Path,
    url: str | None,
    tokenizer: TokenizerCache,
) -> SpikeResult:
    # Before any chunker runs. A warm cache only reads local files; a cold one downloads here.
    tokenizer.prepare()
    res = SpikeResult()
    with falkor_endpoint(url, data_dir) as ep, tempfile.TemporaryDirectory() as tmp:
        log.info("falkordb endpoint", local=ep.is_local, host=ep.host, port=ep.port)
        await run_sdk(ep, sdk_llm, embedder, Path(tmp), res)
        await run_graphiti(ep, graphiti_llm, embedder, res)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="record", choices=[m.value for m in RecordMode])
    ap.add_argument("--local", action="store_true", help="ignore FALKORDB_URL and start a local server")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    conf = ConfigLoader(root / "config")
    graph_cfg = conf.load("graph", GraphConfig)
    models = GraphModels(LLMFactory(LLMSettings()), graph_cfg, root)
    recorder = CallRecorder(root / "tests/fixtures/llm_calls/spike", RecordMode(args.mode))
    embedder = build_embedder(conf.load("memory", MemoryConfig).embedder)
    url = None if args.local else os.environ.get("FALKORDB_URL")
    tokenizer = TokenizerCache.from_config(root, graph_cfg)
    with tempfile.TemporaryDirectory() as data:
        res = asyncio.run(
            run(
                recorder,
                models.sdk_llm(recorder),
                models.graphiti_llm(recorder),
                embedder,
                Path(data),
                url,
                tokenizer,
            )
        )
    # Program output for the person running the spike, same as scripts/record_evals.py.
    print(json.dumps(res.__dict__, indent=1, default=str))
    print(f"model={models.ref()} recorded_hits={recorder.hits} misses={recorder.misses}")


if __name__ == "__main__":
    main()
