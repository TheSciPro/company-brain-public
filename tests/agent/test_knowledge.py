"""Knowledge service and approval matrix: visibility, audit, ambiguity (no topic or client is special)."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

import pytest

from nwb.errors import NotFoundError
from nwb.knowledge.levels import plan_reads
from nwb.knowledge.models import Citation, FactValue, KeyAnswer
from nwb.knowledge.retrieve import GraphPassage
from tests.agent.conftest import Harness

# Scope chain shape for someone on the Brightmart account. Most specific first.
_ASOF = date(2026, 9, 28)
_BRIGHTMART = ["client=ACC-BRM", None]


def test_unknown_question_says_so(harness: Harness) -> None:
    res = harness.c.knowledge.lookup(harness.actor(), "zzz qqq xyzzy")
    assert res.best is None and res.note


def test_amount_banded_action_without_an_amount_returns_candidates(harness: Harness) -> None:
    actor = harness.actor()
    rows = harness.c.approvals.rows(actor.asof, person_id=actor.person_id)
    counts = Counter((r.rule.action, r.rule.region) for r in rows if r.rule.min_amount is not None)
    (action, _), n = counts.most_common(1)[0]
    assert n > 1
    out = harness.c.approvals.who_approves(harness.actor(), action, None, None)
    assert out.rule is None and len(out.candidates) > 1


def test_every_answer_is_audited_with_its_citations(harness: Harness) -> None:
    res = harness.c.knowledge.lookup(harness.actor("NW1103"), "How many days a week can I work from home?")
    assert res.best is not None and res.best.answer is not None
    with harness.c.db.unit_of_work() as uow:
        rows = uow.fetchall("SELECT person_id, tool, citations FROM decision_audits")
    assert rows and rows[-1]["person_id"] == "NW1103"
    assert res.best.answer.citation.id in str(rows[-1]["citations"])


def test_company_search_hides_what_the_user_cannot_see(harness: Harness) -> None:
    """Graphs the caller cannot read in full are not opened, and no unseen-match count is returned."""
    secret = "DEC-2026-005"
    seen: list[str] = []
    closed: set[str] = set()

    class _OpenOnly:
        def retrieve(self, graph: str, question: str, limit: int) -> list[GraphPassage]:
            del question, limit
            seen.append(graph)
            body = secret if _level_of(graph) in closed else "staff handbook excerpt"
            return [GraphPassage(content=body, section="passages", score=0.8)]

    harness.c.company_search._retriever = _OpenOnly()
    probe = harness.c.company_search.search(harness.actor("NW1103"), "Brightmart discount", None, None)
    closed.update(item.source for item in probe.not_searched if item.reason == "access")
    assert closed, "a level the reader cannot wholly see must be left unsearched"
    seen.clear()
    pm = harness.c.company_search.search(harness.actor("NW1103"), "Brightmart discount", None, None)
    fin = harness.c.company_search.search(harness.actor("NW0201"), "Brightmart discount", None, None)
    assert secret not in " ".join(hit.snippet for hit in (*pm.hits, *fin.hits))
    assert "DEC-2026-005" not in {hit.id for hit in pm.hits}
    assert pm.hidden_matches == 0 and fin.hidden_matches == 0
    assert "hidden_matches" not in pm.model_dump()
    for result in (pm, fin):
        note = (result.note or "").lower()
        assert "hidden" not in note
        assert "cannot see" not in note
        assert "there is more" not in note
    assert all(_level_of(graph) not in closed for graph in seen)


def test_weak_match_returns_no_chunks(harness: Harness) -> None:
    """Vector search always has a neighbour. A top score under the config floor is an abstention."""
    floor = harness.c.graph_config.company.evidence_floor
    assert floor > 0
    noise = "UNRELATED-RATE-CLAIM"
    seen: list[str] = []

    class _Weak:
        def retrieve(self, graph: str, question: str, limit: int) -> list[GraphPassage]:
            del question, limit
            seen.append(graph)
            return [GraphPassage(content=noise, section="passages", score=floor / 2)]

    harness.c.company_search._retriever = _Weak()
    found = harness.c.company_search.search(harness.actor("NW1103"), "zzzz quantum pottery", None, None)
    assert seen, "the weak neighbour has to be retrieved before the floor can drop it"
    assert found.hits == []
    blob = str(found.model_dump())
    assert noise not in blob
    assert found.hidden_matches == 0
    assert "hidden_matches" not in found.model_dump()
    note = (found.note or "").lower()
    assert "not enough evidence" in note
    assert "hidden" not in note
    assert "there is more" not in note


def test_company_search_does_not_leak_hidden_match_count(harness: Harness) -> None:
    found = harness.c.company_search.search(harness.actor("NW1103"), "Brightmart discount", None, None)
    note = found.note or ""
    assert found.hidden_matches == 0
    assert "hidden" not in note.lower()
    assert "hidden_matches" not in found.model_dump()


def test_external_auditor_sees_no_internal_discount(harness: Harness) -> None:
    res = harness.c.knowledge.lookup(harness.actor("EXT-AA-01"), "What discount did we agree?", client="Brightmart")
    spoken = " ".join(part for part in (res.note, res.say) if part)
    assert "not on record for you" in spoken
    assert "18" not in spoken
    assert "hidden" not in spoken.lower()
    assert "cannot see" not in spoken.lower()
    assert not re.search(r"\d", spoken)
    assert res.best is None or res.best.answer is None or res.best.answer.value != "18"
    seen = {str(row["key"]) for row in harness.c.fact_book.catalog(_readable(harness, "EXT-AA-01"))}
    assert {c.key for c in res.candidates} <= seen
    hidden = _hidden_discount_key(harness, "EXT-AA-01")
    assert hidden not in {c.key for c in res.candidates}


def _readable(harness: Harness, person: str) -> set[str]:
    actor = harness.actor(person)
    policy = harness.c.access_policy()
    docs = policy.visible_document_ids(actor.person_id, actor.asof)
    return set(docs) | set(policy.visible_record_ids(actor.person_id, actor.asof))


def _hidden_discount_key(harness: Harness, person: str) -> str:
    """The key of the internal 18% discount, which this person cannot read any source of."""
    with harness.c.db.unit_of_work() as uow:
        every = {str(r["id"]) for r in uow.fetchall("SELECT id FROM documents UNION SELECT id FROM records")}
    keys = {str(r["key"]) for r in harness.c.fact_book.catalog(every)}
    seen = {str(r["key"]) for r in harness.c.fact_book.catalog(_readable(harness, person))}
    discount = {
        f.key
        for key in keys - seen
        if "discount" in key
        for f in harness.c.fact_book.history(key, harness.c.settings.company_id)
        if f.value == "18"
    }
    assert len(discount) == 1, "the internal discount must exist and be wholly hidden"
    return discount.pop()


def test_hidden_fact_key_and_label_stay_out_of_lookup_and_history(harness: Harness) -> None:
    actor = harness.actor("EXT-AA-01")
    key = _hidden_discount_key(harness, "EXT-AA-01")
    res = harness.c.knowledge.lookup(actor, key.replace("_", " "), client="Brightmart")
    assert key not in str(res.model_dump())
    with pytest.raises(NotFoundError):
        harness.c.knowledge.history(actor, key)


def _candidate(key: str, score: float, value: str | None, scope: str | None, cite: str | None = None) -> KeyAnswer:
    answer = None
    if value is not None:
        answer = FactValue(
            value=value,
            status="current",
            scope=scope,
            citation=Citation(id=cite or "src", kind="fact", authority="contract"),
        )
    return KeyAnswer(key=key, score=score, asof=_ASOF, answer=answer)


def _answered(harness: Harness, cands: list[KeyAnswer]) -> KeyAnswer | None:
    """The candidate ``KnowledgeService.lookup`` would return as ``best`` after ``_rank``."""
    ranked = harness.c.knowledge._rank(cands, _BRIGHTMART)
    return next((c for c in ranked if c.answer is not None), None)


def test_nikhil_discount_does_not_substitute_another_fact(harness: Harness) -> None:
    best = _answered(
        harness,
        [
            _candidate("discount_pct", 0.492, None, None),
            _candidate("remote_work_allowed", 0.395, "no", "client=ACC-BRM", "MSA-BRM-2026"),
        ],
    )
    assert best is None or best.key == "discount_pct"
    if best is not None and best.answer is not None:
        assert best.answer.value != "18"


def test_claire_discount_with_client_named_abstains(harness: Harness) -> None:
    # Same ranker inputs the review measured once the client argument put the MSA fact in scope.
    best = _answered(
        harness,
        [
            _candidate("discount_pct", 0.492, None, None),
            _candidate("remote_work_allowed", 0.395, "no", "client=ACC-BRM", "MSA-BRM-2026"),
        ],
    )
    assert best is None or best.key == "discount_pct"
    if best is not None and best.answer is not None:
        assert best.answer.citation.id != "MSA-BRM-2026"


def test_search_audience_drops_graphs_the_reader_could_open(harness: Harness) -> None:
    reader = harness.actor("NW1103")
    audience = harness.actor("EXT-AA-01")
    alone = plan_reads(harness.c.db, harness.c.access_policy(), harness.c.classification, reader)
    shared = plan_reads(harness.c.db, harness.c.access_policy(), harness.c.classification, reader, audience)
    assert set(shared.allowed) <= set(alone.allowed)
    narrowed = set(alone.allowed) - set(shared.allowed)
    assert narrowed, "the external audience must close at least one graph the employee could open"
    reasons = {item.level: item.reason for item in shared.decisions}
    assert all(reasons[level] == "audience" for level in narrowed)


def test_document_client_scopes_are_catalog_clients(harness: Harness) -> None:
    ids = {c.id for c in harness.c.knowledge.clients.all()}
    assert ids and all(c.name != c.id for c in harness.c.knowledge.clients.all())
    scopes = harness.c.knowledge.document_scopes()
    used = {p.split("=", 1)[1] for s in scopes for p in s.split(";") if p.startswith("client=")}
    assert used <= ids


def _level_of(graph: str) -> str:
    """``{prefix}{company}_kb_{level}`` -> level. The level is the last segment."""
    marker = "_kb_"
    return graph.rsplit(marker, 1)[-1] if marker in graph else graph
