"""KnowledgeService: natural-language question -> fact keys -> as-of answer for this person.

Facts are read from the per-level FalkorDB graphs. Scope comes from the person's country and active clients.
The first scope with a visible fact wins; the next one is the rule it overrides. Precedence was decided at
write time. A question whose best key has no fact this person can see abstains. It does not substitute a
different key, and it does not say how many sources were skipped.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from datetime import date

from nwb.conf.models import KnowledgeConfig, SourcesConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.errors import NotFoundError
from nwb.identity import Actor
from nwb.knowledge.audit import DecisionAuditor
from nwb.knowledge.clients import ClientDirectory
from nwb.knowledge.ingest import assignment_rows
from nwb.knowledge.keys import FactKeyIndex, KeyMatch
from nwb.knowledge.models import (
    AUTHORITY_LABELS,
    Citation,
    FactHistory,
    FactLookup,
    FactValue,
    FactVersion,
    KeyAnswer,
)
from nwb.knowledge.scopes import scope_chain
from nwb.memory.embeddings import Embedder
from nwb.obs.log import Hop, dataflow, traced
from nwb.ports import AccessPolicy, SqlDatabase
from nwb.temporal.facts import Fact
from nwb.temporal.graph_facts import GraphFactBook

NOT_ON_RECORD = "This is not on record for you. Ask your manager or the document owner."


class KnowledgeService:
    def __init__(
        self,
        db: SqlDatabase,
        policy: AccessPolicy,
        sources: SourcesConfig,
        cfg: KnowledgeConfig,
        embedder: Embedder,
        stopwords: frozenset[str],
        auditor: DecisionAuditor,
        company_id: str,
        facts: GraphFactBook,
    ) -> None:
        self._db = db
        self._policy = policy
        self._src = sources
        self._cfg = cfg
        self._embedder = embedder
        self._stop = stopwords
        self._audit = auditor
        self._about = company_id
        self._facts = facts
        self._indexes: OrderedDict[tuple[str, date], FactKeyIndex] = OrderedDict()
        self.clients = ClientDirectory(db)

    # ---------------------------------------------------------------- key index (per asker; rebuilt after ingest)
    def index(self, actor: Actor, visible: set[str]) -> FactKeyIndex:
        """Keys, labels and quotes from facts whose source this person may read. A hidden fact never gets in."""
        slot = (actor.person_id, actor.asof)
        cached = self._indexes.get(slot)
        if cached is not None:
            self._indexes.move_to_end(slot)
            return cached
        built = FactKeyIndex(self._facts.catalog(visible), self._embedder, self._cfg.resolve, self._stop)
        self._indexes[slot] = built
        while len(self._indexes) > self._cfg.resolve.index_cache:
            self._indexes.popitem(last=False)
        return built

    def invalidate(self) -> None:
        self._indexes.clear()

    def document_scopes(self) -> set[str]:
        return self._facts.document_scopes()

    def scopes_for(self, person_id: str, asof: date) -> dict[str, list[str]]:
        """The person's values per scope dimension, from the directory country and active assignments."""
        out: dict[str, list[str]] = {}
        with self._db.unit_of_work(as_person=person_id, asof=asof) as uow:
            person = DirectoryReader(uow).person(person_id)
            if person is not None and person.country:
                out["country"] = [person.country]
            clients: list[str] = []
            for rtype, spec in self._src.by_role("assignment").items():
                rows = uow.fetchall("SELECT payload FROM records WHERE record_type = :t", {"t": rtype})
                for a in assignment_rows(spec, (json.loads(r["payload"]) for r in rows)):
                    if (
                        a["person"] == person_id
                        and a["client"]
                        and a["active"]
                        and (a["start"] is None or a["start"] <= asof)
                        and (a["end"] is None or a["end"] >= asof)
                    ):
                        clients.append(str(a["client"]))
        if clients:
            out["client"] = sorted(set(clients))
        return out

    @traced(step="knowledge.lookup")
    def lookup(self, actor: Actor, question: str, client: str | None = None, on_date: date | None = None) -> FactLookup:
        on = on_date or actor.asof
        scopes = self.scopes_for(actor.person_id, actor.asof)
        if client:
            named = self.clients.resolve(client)
            if named is None:
                raise NotFoundError(f"no client matching {client!r}")
            scopes["client"] = [named.id]
        elif (in_question := self.clients.resolve(question)) is not None:
            # The question names a client the model did not pass: scope to it rather than to the asker's own
            # accounts. Access is unchanged: a fact the asker may not see still abstains.
            scopes["client"] = [in_question.id]
        chain = scope_chain(scopes, self._src.scope_dimensions)
        visible = self._sources(actor)  # access is judged today, from document and record RLS
        matches = self.index(actor, visible).resolve(question)
        candidates = self._rank([self._answer(m, chain, on, visible) for m in matches], chain)
        best = next((c for c in candidates if c.answer is not None), None)
        note, say = self._wording(matches, best, actor)
        cites = [v.citation for v in (best.answer, best.overridden_general, best.replaced) if v] if best else []
        did = self._audit.record(
            actor,
            tool="company_fact",
            question=f"{question} client={client or '-'} on={on}",
            answer=say or note or "",
            citations=cites,
            on_date=on,
        )
        dataflow(Hop.RETRIEVAL, kind="company_fact", keys=[m.key for m in matches], best=best.key if best else None)
        return FactLookup(
            question=question,
            say=say,
            stop=True,
            client=client,
            candidates=candidates,
            decision_id=did,
            note=note,
        )

    def _wording(self, matches: list[KeyMatch], best: KeyAnswer | None, actor: Actor) -> tuple[str | None, str | None]:
        # No match and no visible answer read the same, so the reply never shows that a hidden fact exists.
        if not matches or best is None or best.answer is None:
            note = self.not_on_record(actor)
            return note, note
        return None, _say(best, self._sentence(best))

    def _sentence(self, best: KeyAnswer) -> str:
        """The configured reply template for this key (sources.yaml ``say``), with scope values filled in."""
        answer = best.answer
        assert answer is not None
        template = next(
            (f.say for r in self._src.records.values() for f in r.facts if f.field == best.key and f.say),
            "The {label} is {value}.",
        )
        drop = set(self._src.unit_words.get(answer.unit or "", ()))
        label = " ".join(w for w in (best.label or best.key).split() if w.lower() not in drop)
        values: dict[str, str] = {"value": _shown(answer), "label": label}
        for part in (answer.scope or "").split(";"):
            dim, _, val = part.partition("=")
            if dim and val:
                named = self.clients.get(val) if dim == "client" else None
                values[dim] = named.name if named else val
        try:
            return template.format_map(values)
        except KeyError:  # a placeholder this fact has no value for: fall back rather than fail the answer
            return "The {label} is {value}.".format_map(values)

    def not_on_record(self, actor: Actor) -> str:
        """The abstention reply: not on record for this person, and who to ask (their manager when known)."""
        with self._db.unit_of_work() as uow:
            mgr = DirectoryReader(uow).manager(actor.person_id, actor.asof)
        if mgr is None:
            return NOT_ON_RECORD
        return f"This is not on record for you. Ask {mgr.name}, your manager, or the document owner."

    def _rank(self, cands: list[KeyAnswer], chain: list[str | None]) -> list[KeyAnswer]:
        """Order candidates. If the best-matching key has no visible fact, return that key and nothing else.

        A different key that happens to be visible is not a substitute. Among keys that do have an answer and
        score within ``specific_margin`` of the best, the one at the asker's most specific scope wins.
        """
        if not cands:
            return cands
        top = max(c.score for c in cands)
        leaders = [c for c in cands if c.score >= top]
        if not any(c.answer is not None for c in leaders):
            return leaders[: self._cfg.resolve.shown]
        cut = top * self._cfg.resolve.specific_margin
        rank_of = {label: rank for rank, label in AUTHORITY_LABELS.items()}

        def order(c: KeyAnswer) -> tuple[int, int, float]:
            answer = c.answer
            depth = chain.index(answer.scope) if answer is not None and answer.scope in chain else len(chain)
            authority = rank_of.get(answer.citation.authority or "", 0) if answer is not None else 0
            # Most specific scope, then authority (a client contract beats a near-equal policy).
            # A hidden top key never reaches here: that case abstains instead of promoting another key.
            return depth, -authority, -c.score

        near = sorted((c for c in cands if c.score >= cut and c.answer), key=order)
        rest = [c for c in cands if c not in near]
        return (near + rest)[: self._cfg.resolve.shown]

    def _answer(self, match: KeyMatch, chain: list[str | None], on: date, visible: set[str]) -> KeyAnswer:
        live = self._facts.live_statuses
        found: list[Fact] = []
        for scope in chain:
            hit = next(iter(self._facts.valid_at(match.key, self._about, scope, on, live, visible)), None)
            if hit is not None:
                found.append(hit)
            if len(found) == 2:
                break
        winner = found[0] if found else None
        replaced = self._facts.get(winner.supersedes_id) if winner and winner.supersedes_id else None
        if replaced is not None and replaced.src_id not in visible:
            replaced = None
        contested = [
            f
            for f in self._facts.contested(match.key, self._about, winner.scope if winner else None, visible)
            if f.valid_from <= on
        ]

        def view(fact: Fact | None) -> FactValue | None:
            if fact is None or fact.src_id not in visible:
                return None
            return FactValue(
                value=fact.value,
                unit=fact.unit or match.unit,
                status=fact.status,
                scope=fact.scope,
                withdrawn=fact.valid_to is not None,
                citation=_cite(fact),
            )

        return KeyAnswer(
            key=match.key,
            label=match.label,
            score=round(match.score, 3),
            asof=on,
            scopes_tried=[s or "" for s in chain],
            answer=view(winner),
            overridden_general=view(found[1]) if len(found) > 1 else None,
            replaced=view(replaced),
            contested=[v for f in contested if (v := view(f))],
        )

    def _sources(self, actor: Actor) -> set[str]:
        """Document and record ids this person may read today. Postgres RLS (or the SQLite CTE) decides."""
        docs = self._policy.visible_document_ids(actor.person_id, actor.asof)
        recs = self._policy.visible_record_ids(actor.person_id, actor.asof)
        return set(docs) | set(recs)

    @traced(step="knowledge.history")
    def history(self, actor: Actor, key: str) -> FactHistory:
        visible = self._sources(actor)
        facts = self._facts.history(key, self._about, visible)
        if not facts:
            raise NotFoundError(NOT_ON_RECORD)
        return FactHistory(
            key=key,
            label=next((f.label for f in facts if f.label), None),
            versions=[
                FactVersion(
                    value=f.value,
                    unit=f.unit,
                    status=f.status,
                    scope=f.scope,
                    valid_from=f.valid_from,
                    valid_to=f.valid_to,
                    recorded_at=f.recorded_at,
                    withdrawn=f.valid_to is not None,
                    citation=_cite(f),
                )
                for f in facts
            ],
        )


def _shown(answer: FactValue) -> str:
    unit = answer.unit or ""
    if unit == "%":
        return f"{answer.value}%"
    return f"{answer.value} {unit}" if unit else answer.value


def _say(best: KeyAnswer, sentence: str) -> str:
    """``say`` is shown to the user as the reply (direct_reply_tools): the full sentence, then the citation."""
    answer = best.answer
    assert answer is not None
    sentence = f"{sentence.rstrip('.')} [{answer.citation.id}]."
    if answer.withdrawn:
        sentence += " That fact was later withdrawn."
    if best.overridden_general is not None:
        sentence += f" It overrides {best.overridden_general.value}."
    elif best.replaced is not None:
        sentence += f" It replaced {best.replaced.value}."
    return sentence


def _cite(fact: Fact) -> Citation:
    return Citation(
        id=fact.src_id,
        kind="fact",
        title=fact.title,
        quote=(fact.quote or "")[:240] or None,
        valid_from=fact.valid_from,
        valid_to=fact.valid_to,
        authority=AUTHORITY_LABELS.get(int(fact.authority)),
        recorded_at=fact.recorded_at,
        level=fact.level,
        graph=fact.graph,
        node_id=fact.id,
        node_label=fact.node_label,
        query=fact.query,
    )
