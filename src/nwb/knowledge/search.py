"""Company search over the per-level FalkorDB graphs, via GraphRAG-SDK retrieval.

The keyword scan of every document and record is gone. A question is sent only
to graphs the caller (and, when set, the reply audience) may read in full.
A top score under ``graph.yaml`` ``evidence_floor`` returns no passages. Vector
search always has neighbours, so the floor runs here, before any chunk is
returned. Sources that were not searched are named. Counts of unseen matches
are not.
"""

from __future__ import annotations

from nwb.conf.models import ClassificationConfig, CompanySearchConfig, GraphConfig
from nwb.identity import Actor
from nwb.knowledge.levels import plan_reads
from nwb.knowledge.models import CompanyHit, CompanySearchResult, NotSearched
from nwb.knowledge.retrieve import CompanyGraphRetriever, GraphPassage, GraphUnavailableError
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.ports import AccessPolicy, SqlDatabase

log = get_logger(__name__)


class CompanySearch:
    def __init__(
        self,
        db: SqlDatabase,
        policy: AccessPolicy,
        classification: ClassificationConfig,
        graph_cfg: GraphConfig,
        search_cfg: CompanySearchConfig,
        retriever: CompanyGraphRetriever,
        *,
        prefix: str = "",
    ) -> None:
        self._db = db
        self._policy = policy
        self._cls = classification
        self._graph = graph_cfg
        self._cfg = search_cfg
        self._retriever = retriever
        self._prefix = prefix

    def close(self) -> None:
        """Stop a local FalkorDB this search started. A remote endpoint has nothing to stop."""
        closer = getattr(self._retriever, "close", None)
        if closer is not None:
            closer()

    @traced(step="knowledge.search")
    def search(
        self,
        actor: Actor,
        query: str,
        kind: str | None,
        limit: int | None,
        audience: Actor | None = None,
    ) -> CompanySearchResult:
        text = query.strip()
        if len(text) < 2:
            return CompanySearchResult(query=query, hits=[], note="query has no searchable words")
        cap = _cap(limit, self._cfg, self._graph)
        plan = plan_reads(self._db, self._policy, self._cls, actor, audience)
        dataflow(
            Hop.RETRIEVAL,
            kind="company_search",
            phase="plan",
            company=actor.company_id,
            person=actor.person_id,
            audience=audience.person_id if audience is not None else None,
            allow=list(plan.allowed),
            skip=[{"source": item.level, "reason": item.reason} for item in plan.skipped()],
        )
        skipped = [NotSearched(source=item.level, reason=item.reason or "access") for item in plan.skipped()]
        found: list[tuple[str, GraphPassage]] = []
        searched: list[str] = []
        for level in plan.allowed:
            graph = self._graph.company_graph(actor.company_id, level, prefix=self._prefix)
            try:
                passages = self._retriever.retrieve(graph, text, cap)
            except GraphUnavailableError:
                skipped.append(NotSearched(source=level, reason="unavailable"))
                dataflow(Hop.RETRIEVAL, kind="company_search", phase="unavailable", graph=graph, level=level)
                continue
            searched.append(level)
            dataflow(
                Hop.RETRIEVAL,
                kind="company_search",
                phase="graph",
                graph=graph,
                level=level,
                passages=len(passages),
            )
            found.extend((level, passage) for passage in passages)
        found.sort(key=_passage_order)
        floor = self._graph.company.evidence_floor
        chosen = [item for item in found if _clears(item[1], floor)][:cap]
        minimum = self._graph.company.min_evidence
        abstained = (floor > 0 and not chosen) or (minimum > 0 and len(chosen) < minimum)
        width = self._cfg.snippet_chars
        hits = (
            []
            if abstained
            else [
                _hit(
                    level,
                    self._graph.company_graph(actor.company_id, level, prefix=self._prefix),
                    passage,
                    index,
                    width,
                )
                for index, (level, passage) in enumerate(chosen)
            ]
        )
        note = _note(searched, skipped, abstained, kind)
        dataflow(
            Hop.RETRIEVAL,
            kind="company_search",
            phase="result",
            hits=len(hits),
            abstained=abstained,
            searched=searched,
        )
        return CompanySearchResult(query=query, hits=hits, searched=searched, not_searched=skipped, note=note)


def _clears(passage: GraphPassage, floor: float) -> bool:
    """A missing score does not clear a positive floor. ``0`` turns the floor off."""
    if floor <= 0:
        return True
    return passage.score is not None and passage.score >= floor


def _passage_order(item: tuple[str, GraphPassage]) -> tuple[float, str, str]:
    level, passage = item
    score = passage.score if passage.score is not None else -1.0
    return (-score, level, passage.section or "")


def _cap(limit: int | None, search_cfg: CompanySearchConfig, graph_cfg: GraphConfig) -> int:
    requested = limit if limit is not None else search_cfg.default_limit
    return max(1, min(requested, search_cfg.max_limit, graph_cfg.company.retrieve_limit))


def _hit(level: str, graph: str, passage: GraphPassage, index: int, width: int) -> CompanyHit:
    section = passage.section or "context"
    return CompanyHit(
        id=f"{level}:{section}:{index}",
        kind="graph",
        title=section,
        snippet=_snippet(passage.content, width),
        classification=level,
        score=round(passage.score if passage.score is not None else 0.0, 3),
        graph=graph,
    )


def _snippet(text: str, width: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return flat[:width].rsplit(" ", 1)[0] if " " in flat[:width] else flat[:width]


def _note(
    searched: list[str],
    skipped: list[NotSearched],
    abstained: bool,
    kind: str | None,
) -> str | None:
    parts: list[str] = []
    if abstained:
        parts.append("Not enough evidence to answer.")
    if searched:
        parts.append("Searched: " + ", ".join(searched) + ".")
    if skipped:
        named = ", ".join(f"{item.source} ({item.reason})" for item in skipped)
        parts.append("Did not search: " + named + ".")
    if kind:
        parts.append("Kind was ignored; each allowed graph is searched as a whole.")
    text = " ".join(parts).strip()
    return text or None
