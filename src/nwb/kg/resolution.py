"""Run GraphRAG-SDK ``finalize()`` and file what it would not merge.

People who share an email or id are linked and kept as two nodes. A probable duplicate at or
above the config threshold becomes one approvals-inbox suggestion. A model is not asked to merge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from graphrag_sdk import GraphRAG
from graphrag_sdk.core.models import FinalizeResult
from graphrag_sdk.core.providers.base import LLMInterface

from nwb.actions.service import ActionService
from nwb.conf.models import ResolutionConfig
from nwb.kg.identity import IdentityLink, Record, Suggestion, parse_probable, record_values, suggestions_for
from nwb.kg.identity import link_groups as identity_links
from nwb.obs.log import Hop, dataflow, get_logger, scrub_str

log = get_logger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_YES_NO = re.compile(r"\b(yes|no)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    entities_deduplicated: int
    links: int
    suggestions: int
    filed: int


class SuggestionSink(Protocol):
    def file(self, suggestion: Suggestion, *, graph: str, reviewer: str, asof: date) -> str: ...


class ApprovalInbox:
    """File a suggestion through the existing approval queue. Approving does not merge."""

    def __init__(self, actions: ActionService, kind: str, proposed_by: str) -> None:
        self._actions = actions
        self._kind = kind
        self._by = proposed_by

    def file(self, suggestion: Suggestion, *, graph: str, reviewer: str, asof: date) -> str:
        view = self._actions.propose(
            kind_name=self._kind,
            requester=reviewer,
            target=None,
            title=f"Possible duplicate: {suggestion.left_name} ~ {suggestion.right_name}",
            args={
                "message": (
                    f"These may be the same {suggestion.label}. Nothing was merged. Reason: {suggestion.reason}."
                ),
                "label": suggestion.label,
                "left_id": suggestion.left_id,
                "right_id": suggestion.right_id,
                "score": suggestion.score,
                "reason": suggestion.reason,
                "graph": graph,
            },
            asof=asof,
            proposed_by=self._by,
            dedupe_key=f"dup:{graph}:{suggestion.label}:{suggestion.left_id}|{suggestion.right_id}",
        )
        return view.action_id


async def finalize_graph(rag: GraphRAG) -> FinalizeResult:
    """Exact-name dedup plus a probable-duplicate report. No model, so nothing is merged on a guess."""
    return await rag.finalize(resolve=False, judge=False)


async def apply_resolution(
    rag: GraphRAG,
    finalized: FinalizeResult,
    cfg: ResolutionConfig,
    *,
    graph: str,
    sink: SuggestionSink | None = None,
    reviewer: str | None = None,
    asof: date | None = None,
) -> ResolutionOutcome:
    """Link shared emails and ids, and file probable duplicates that clear the threshold."""
    nodes = await _nodes(rag, cfg, list(finalized.probable_duplicates))
    links = identity_links(nodes, cfg)
    for link in links:
        await _write_link(rag, link, cfg)
    pending = suggestions_for(list(finalized.probable_duplicates), nodes, cfg)
    filed = 0
    if pending and sink is not None and reviewer:
        day = asof or date.today()
        for suggestion in pending:
            sink.file(suggestion, graph=graph, reviewer=reviewer, asof=day)
            filed += 1
    elif pending:
        log.info("duplicate suggestions not filed", graph=graph, suggestions=len(pending))
    dataflow(
        Hop.INGEST,
        stage="resolution",
        graph=graph,
        deduplicated=finalized.entities_deduplicated,
        links=len(links),
        suggestions=len(pending),
        filed=filed,
    )
    log.info(
        "resolution applied",
        graph=graph,
        deduplicated=finalized.entities_deduplicated,
        links=len(links),
        suggestions=len(pending),
        filed=filed,
    )
    return ResolutionOutcome(finalized.entities_deduplicated, len(links), len(pending), filed)


async def confirm_same(llm: LLMInterface, left: str, right: str, label: str) -> str:
    """Ask a model yes or no, once. The answer never merges; callers still use the config rules."""
    prompt = (
        "Reply with one word, yes or no. "
        f"Do these two {label} names probably refer to one thing: {left!r} and {right!r}?"
    )
    try:
        # The SDK rejects max_retries < 1. One attempt is the single yes/no; it does not merge.
        response = await llm.ainvoke(prompt, max_retries=1, timeout=45, max_tokens=256)
    except Exception as exc:
        raise RuntimeError(scrub_str(str(exc))) from None
    match = _YES_NO.search(response.content or "")
    if match is None:
        raise RuntimeError("model did not answer yes or no")
    word = match.group(1).lower()
    log.info("duplicate confirm", label=label, answer=word)
    return word


async def _nodes(rag: GraphRAG, cfg: ResolutionConfig, lines: list[str]) -> list[Record]:
    labels = {rule.label for rule in cfg.identities}
    for line in lines:
        parsed = parse_probable(line)
        if parsed is not None and _IDENT.match(parsed.label):
            labels.add(parsed.label)
    nodes: list[Record] = []
    for label in sorted(labels):
        if not _IDENT.match(label):
            log.warning("skipping label that is not an identifier", label=label)
            continue
        rows = await rag.query(
            f"MATCH (e:`{label}`) RETURN e.id, e.name, e.entity_key, properties(e)",
        )
        rule = cfg.rule_for(label)
        for row in rows:
            if not row or row[0] in (None, ""):
                continue
            props = _props(row[3] if len(row) > 3 else None)
            if len(row) > 2 and row[2] not in (None, ""):
                props.setdefault("entity_key", row[2])
            values = record_values(props, rule, cfg.name) if rule is not None else {}
            nodes.append(Record(str(row[0]), label, str(row[1] or ""), values))
    return nodes


async def _write_link(rag: GraphRAG, link: IdentityLink, cfg: ResolutionConfig) -> None:
    rel = cfg.link.type
    await rag.query(
        f"MATCH (a:__Entity__ {{id: $a}}), (b:__Entity__ {{id: $b}}) "
        f"MERGE (a)-[r:{rel} {{field: $field}}]->(b) "
        "SET r.origin = $origin, r.confidence = $confidence",
        {
            "a": link.left_id,
            "b": link.right_id,
            "field": link.field,
            "origin": cfg.link.origin,
            "confidence": cfg.scores.exact_link,
        },
    )


def _props(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
    if isinstance(raw, list):
        out: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = item[1]
        return out
    return {}
