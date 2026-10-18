"""Load configured company sources into one FalkorDB graph per access level.

Tabular sources go through GraphRAG-SDK ``TableMapping`` (no model). Prose is extracted
under the approved ontology only: a type or relation that file does not name is not
loaded. Discovery and approval live in ``nwb.kg.ontology``. Dedup is ``finalize()``: exact names
collapse, probable duplicates are reported and not merged. When a resolution config is passed,
people who share an email or id are linked and the rest of those reports become inbox suggestions.
Every relationship is stamped EXTRACTED or INFERRED before those links are written, so the
stamp does not relabel them. Counts are read back from the graph; ingest return values are
not the report, because re-ingesting an existing graph returns zeros.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from graphrag_sdk import Column, ConnectionConfig, GraphRAG, Link, Ontology, TableMapping
from graphrag_sdk.core.providers.base import LLMInterface
from graphrag_sdk.ingestion.extraction_strategies.entity_extractors import LLMExtractor
from graphrag_sdk.ingestion.extraction_strategies.graph_extraction import GraphExtraction

from nwb.conf.models import EdgeOriginConfig, GraphConfig, ResolutionConfig, TableDecl
from nwb.errors import DataLoadError
from nwb.ingest.acl import classification_config
from nwb.kg.catalog import LevelBundle, materialise
from nwb.kg.embedding import SdkEmbedder
from nwb.kg.ontology import ApprovedOnly, restrict
from nwb.kg.resolution import ResolutionOutcome, SuggestionSink, apply_resolution, finalize_graph
from nwb.kg.server import FalkorEndpoint
from nwb.memory.embeddings import Embedder
from nwb.obs.log import Hop, dataflow, get_logger, log_step

log = get_logger(__name__)

# Index builds on a few thousand entities exceed the SDK's 10s default.
_QUERY_TIMEOUT_MS = 120_000
_STRUCTURAL = ("MENTIONED_IN", "PART_OF", "NEXT_CHUNK")
_REL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class LevelCounts:
    level: str
    graph: str
    entities: int
    edges: int
    origins: dict[str, int]
    table_rows: int
    prose_docs: int
    entities_deduplicated: int = 0
    links: int = 0
    suggestions: int = 0
    filed: int = 0


@dataclass
class LoadReport:
    company: str
    prefix: str
    levels: list[LevelCounts] = field(default_factory=list)
    llm: str = "offline"
    embedder: str = "hashing"

    def as_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "prefix": self.prefix,
            "llm": self.llm,
            "embedder": self.embedder,
            "levels": [level.__dict__ for level in self.levels],
        }


async def load_company(
    cfg: GraphConfig,
    *,
    data_dir: Path,
    company: str,
    endpoint: FalkorEndpoint,
    llm: LLMInterface,
    embedder: Embedder,
    work_dir: Path,
    prefix: str = "",
    max_rows: int | None = None,
    max_prose: int = 0,
    tables_only: bool = False,
    llm_phases: bool = False,
    llm_label: str = "offline",
    resolution: ResolutionConfig | None = None,
    suggestions: SuggestionSink | None = None,
    reviewer: str | None = None,
    asof: date | None = None,
    ontology: Ontology | None = None,
    ontology_path: Path | None = None,
) -> LoadReport:
    """Build a fresh graph per access level. ``prefix`` is prepended to every graph name.

    ``ontology`` or ``ontology_path`` is the approved schema. Labels outside it are dropped
    before ingest.
    """
    load = cfg.company.load
    if load is None:
        raise ValueError("graph.yaml company.load is required")
    levels = tuple(level.code for level in classification_config().levels)
    with log_step("kg.load"):
        bundles = materialise(
            data_dir,
            load,
            work_dir,
            levels,
            max_rows=max_rows,
            max_prose=max_prose,
            tables_only=tables_only,
        )
        approved = _approved_ontology(ontology, ontology_path)
        report = LoadReport(company=company, prefix=prefix, llm=llm_label, embedder=embedder.model)
        for bundle in bundles:
            graph = cfg.company_graph(company, bundle.level, prefix=prefix)
            counts = await _load_level(
                cfg,
                bundle,
                endpoint,
                llm,
                embedder,
                graph,
                approved,
                llm_phases=llm_phases,
                resolution=resolution,
                suggestions=suggestions,
                reviewer=reviewer,
                asof=asof,
            )
            report.levels.append(counts)
            dataflow(
                Hop.INGEST,
                stage="graph",
                graph=graph,
                level=bundle.level,
                entities=counts.entities,
                edges=counts.edges,
                origins=counts.origins,
                links=counts.links,
                suggestions=counts.suggestions,
            )
            log.info(
                "level loaded",
                graph=graph,
                level=bundle.level,
                entities=counts.entities,
                edges=counts.edges,
                origins=counts.origins,
                deduplicated=counts.entities_deduplicated,
                links=counts.links,
                suggestions=counts.suggestions,
                filed=counts.filed,
            )
        return report


async def _load_level(
    cfg: GraphConfig,
    bundle: LevelBundle,
    endpoint: FalkorEndpoint,
    llm: LLMInterface,
    embedder: Embedder,
    graph: str,
    approved: Ontology,
    *,
    llm_phases: bool,
    resolution: ResolutionConfig | None,
    suggestions: SuggestionSink | None,
    reviewer: str | None,
    asof: date | None,
) -> LevelCounts:
    load = cfg.company.load
    assert load is not None
    ontology = ontology_for_level(approved, bundle)
    previous = Path.cwd()
    os.chdir(bundle.directory)
    try:
        conn = ConnectionConfig(
            host=endpoint.host,
            port=endpoint.port,
            username=endpoint.username,
            password=endpoint.password,
            ssl=endpoint.ssl,
            graph_name=graph,
            query_timeout_ms=_QUERY_TIMEOUT_MS,
        )
        async with GraphRAG(
            connection=conn,
            llm=llm,
            embedder=SdkEmbedder(embedder),
            embedding_dimension=embedder.dim,
            ontology=ontology,
            enable_cypher=cfg.company.enable_cypher,
        ) as rag:
            kept = {mapping.source for mapping in ontology.tables}
            for table in bundle.tables:
                if table.source_name not in kept:
                    continue
                await rag.ingest(table.source_name, max_concurrency=cfg.company.max_concurrency)
            if bundle.prose:
                extractor = ApprovedOnly(GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm)), ontology)
                for doc in bundle.prose:
                    await rag.ingest(
                        text=doc.text,
                        document_id=doc.document_id,
                        extractor=extractor,
                        max_concurrency=cfg.company.max_concurrency,
                    )
            finalized = await finalize_graph(rag)
            await _stamp(rag, cfg.company.edge_origin)
            outcome = ResolutionOutcome(finalized.entities_deduplicated, 0, 0, 0)
            if resolution is not None:
                outcome = await apply_resolution(
                    rag,
                    finalized,
                    resolution,
                    graph=graph,
                    sink=suggestions,
                    reviewer=reviewer,
                    asof=asof,
                )
            if llm_phases:
                log.info("finalize dedup does not merge on a model guess; probable duplicates are suggestions")
            entities, edges, origins = await _counts(rag)
    finally:
        os.chdir(previous)
    return LevelCounts(
        level=bundle.level,
        graph=graph,
        entities=entities,
        edges=edges,
        origins=origins,
        table_rows=bundle.table_rows,
        prose_docs=len(bundle.prose),
        entities_deduplicated=outcome.entities_deduplicated,
        links=outcome.links,
        suggestions=outcome.suggestions,
        filed=outcome.filed,
    )


def ontology_for_level(approved: Ontology, bundle: LevelBundle) -> Ontology:
    """Approved labels, plus this level's tables when those labels are approved."""
    gated = restrict(approved, approved)
    allowed_entities = {entity.label for entity in gated.entities}
    allowed_relations = {relation.label for relation in gated.relations}
    tables: list[TableMapping] = []
    for table in bundle.tables:
        mapping = _mapping(table.decl)
        if mapping.label not in allowed_entities:
            log.info(
                "skipping table; label is not in the approved ontology",
                label=mapping.label,
                source=table.decl.path,
            )
            continue
        links = [link for link in mapping.links if link.type in allowed_relations and link.to in allowed_entities]
        dropped = len(mapping.links) - len(links)
        if dropped:
            log.info(
                "dropping links the approved ontology does not name",
                source=table.decl.path,
                dropped=dropped,
            )
        tables.append(
            TableMapping(
                source=mapping.source,
                label=mapping.label,
                key=mapping.key,
                name=mapping.name,
                properties=dict(mapping.properties),
                links=links,
                standalone=mapping.standalone and not links,
            )
        )
    return Ontology(entities=list(gated.entities), relations=list(gated.relations), tables=tables)


def _approved_ontology(ontology: Ontology | None, path: Path | None) -> Ontology:
    if ontology is not None:
        return ontology
    if path is None or not path.is_file():
        raise DataLoadError("an approved ontology is required; discover and approve one before loading")
    return Ontology.from_file(str(path))


def _mapping(decl: TableDecl) -> TableMapping:
    return TableMapping(
        source=decl.source_name,
        label=decl.label,
        key=decl.key,
        name=decl.name,
        properties={prop: Column(prop) for prop in decl.properties},
        links=[Link(link.type, to=link.to, by=link.by, name=link.name) for link in decl.links],
        standalone=decl.standalone and not decl.links,
    )


async def _stamp(rag: GraphRAG, origin: EdgeOriginConfig) -> None:
    """Mark each edge EXTRACTED or INFERRED. SAME_AS (and any configured type) is inferred."""
    inferred = list(origin.inferred_types)
    await rag.query(
        "MATCH ()-[r:RELATES]->() WHERE r.rel_type IN $inferred SET r.origin = $origin, r.confidence = $confidence",
        {"inferred": inferred, "origin": origin.inferred, "confidence": origin.inferred_confidence},
    )
    await rag.query(
        "MATCH ()-[r:RELATES]->() WHERE NOT r.rel_type IN $inferred SET r.origin = $origin, r.confidence = $confidence",
        {"inferred": inferred, "origin": origin.extracted, "confidence": origin.extracted_confidence},
    )
    for rel in inferred:
        await rag.query(
            f"MATCH ()-[r:{_rel(rel)}]->() SET r.origin = $origin, r.confidence = $confidence",
            {"origin": origin.inferred, "confidence": origin.inferred_confidence},
        )
    for rel in _STRUCTURAL:
        await rag.query(
            f"MATCH ()-[r:{_rel(rel)}]->() SET r.origin = $origin, r.confidence = $confidence",
            {"origin": origin.extracted, "confidence": origin.extracted_confidence},
        )


def _rel(name: str) -> str:
    """Relationship types are interpolated into Cypher, so only config identifiers are allowed."""
    if not _REL_NAME.match(name):
        raise ValueError(f"refusing relationship type {name!r}")
    return name


async def _counts(rag: GraphRAG) -> tuple[int, int, dict[str, int]]:
    entities = await rag.query("MATCH (e:__Entity__) RETURN count(e)")
    edges = await rag.query("MATCH ()-[r]->() RETURN count(r)")
    grouped = await rag.query("MATCH ()-[r]->() RETURN r.origin, count(r)")
    origins = {str(row[0]): int(row[1]) for row in grouped if row and row[0] is not None}
    return _cell(entities), _cell(edges), origins


def _cell(rows: list[list[Any]]) -> int:
    if not rows or rows[0][0] is None:
        return 0
    return int(rows[0][0])
