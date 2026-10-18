"""Who approves X for me: the delegation-of-authority matrix as structured rows + the reporting chain as of D.

Rows come from records whose catalog role is ``approval_matrix`` (field mapping in ``sources.yaml``); they are also
projected into the graph as ``(:DoaRule)`` nodes. Nothing is parsed out of English sentences:

- action: content-word overlap between the request and each row's action text (ties -> ask);
- region: the requester's country from the directory (ISO-normalised); the catalog says which value means "any";
- amount: the row's numeric band;
- approver: the row's approver text resolved against *data* — a directory grade it names (grades and their order are
  read from the directory), a title or title acronym it names (nearest in the reporting chain first, then anyone),
  else the chain is returned for the model to reason over. The requester is never their own approver.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from nwb.conf.models import ApprovalsConfig, RecordSource, SourcesConfig
from nwb.db.repo_directory import DirectoryReader, PersonView
from nwb.identity import Actor
from nwb.knowledge.audit import DecisionAuditor
from nwb.knowledge.ingest import as_date
from nwb.knowledge.keys import terms
from nwb.knowledge.models import ApproverAnswer, Citation, DoaRule
from nwb.obs.log import Hop, dataflow, traced
from nwb.ports import SqlDatabase
from nwb.util.geo import normalize_country

_WORD = re.compile(r"[A-Za-z0-9]+")
_NUM = re.compile(r"\d+")


def natural_key(s: str) -> tuple[str, int]:
    m = _NUM.search(s)
    return (s[: m.start()] if m else s, int(m.group()) if m else -1)


def acronym(title: str) -> str:
    """'Chief Financial Officer' -> 'CFO'; 'HR Business Partner' -> 'HRBP' (all-caps words are kept whole)."""
    return "".join(w if w.isupper() and len(w) > 1 else w[0].upper() for w in _WORD.findall(title))


@dataclass(frozen=True, slots=True)
class _Row:
    rule: DoaRule
    record_id: str
    source_doc: str | None
    quote: str


class ApprovalMatrix:
    def __init__(
        self,
        db: SqlDatabase,
        sources: SourcesConfig,
        cfg: ApprovalsConfig,
        auditor: DecisionAuditor,
        stopwords: frozenset[str],
    ) -> None:
        self._db = db
        self._specs = sources.by_role("approval_matrix")
        self._cfg = cfg
        self._audit = auditor
        self._stop = stopwords

    # ---------------------------------------------------------------- rows
    def rows(self, on: date, *, person_id: str) -> list[_Row]:
        """Matrix rows this person may read on ``on``. Anonymous reads see nothing under RLS."""
        out: list[_Row] = []
        with self._db.unit_of_work(as_person=person_id, asof=on) as uow:
            for rtype, spec in self._specs.items():
                for r in uow.fetchall(
                    "SELECT id, source_id, payload FROM records WHERE record_type = :t ORDER BY source_id", {"t": rtype}
                ):
                    row = self._row(spec, str(r["id"]), str(r["source_id"]), json.loads(r["payload"]))
                    lo, hi = row.rule.valid_from, as_date(json.loads(r["payload"]).get(spec.fields.get("valid_to", "")))
                    if (lo is None or lo <= on) and (hi is None or hi >= on):
                        out.append(row)
        return out

    def _row(self, spec: RecordSource, record_id: str, source_id: str, item: dict[str, Any]) -> _Row:
        def num(field: str) -> float | None:
            v = item.get(spec.f(field)) if field in spec.fields else None
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        region = str(item.get(spec.f("region")) or "") if "region" in spec.fields else ""
        approver = " / ".join(str(item[a]) for a in spec.fs("approvers") if item.get(a))
        rule = DoaRule(
            rule_id=source_id,
            action=str(item.get(spec.f("action")) or ""),
            region="" if region in spec.any_region else (normalize_country(region) or ""),
            min_amount=num("min"),
            max_amount=num("max"),
            unit=(str(item.get(spec.f("unit"))) or None) if "unit" in spec.fields else None,
            approver_phrase=approver,
            valid_from=as_date(item.get(spec.f("valid_from"))) if "valid_from" in spec.fields else None,
        )
        doc = (
            str(item.get(spec.f("source_doc")))
            if "source_doc" in spec.fields and item.get(spec.f("source_doc"))
            else None
        )
        ver = item.get(spec.f("source_version")) if "source_version" in spec.fields else None
        quote = f"{rule.action} | {region} | {rule.min_amount}-{rule.max_amount} | {approver}"
        return _Row(rule, record_id, f"{doc}@{ver}" if doc and ver else doc, quote)

    # ---------------------------------------------------------------- matching
    def _pick(
        self, rows: list[_Row], action: str, region: str | None, amount: float | None
    ) -> tuple[list[_Row], list[str]]:
        want = terms(action, self._stop)
        score: dict[str, tuple[int, float]] = {}
        for r in rows:
            have = terms(r.rule.action, self._stop)
            shared = len(want & have)
            score[r.rule.action] = (shared, shared / len(want | have) if want | have else 0.0)
        top = max(score.values(), default=(0, 0.0))
        actions = sorted(a for a, s in score.items() if s == top and s[0] > 0)
        if len(actions) != 1:
            return [], actions or sorted(score)[:12]
        same = [r for r in rows if r.rule.action == actions[0]]
        regional = [r for r in same if region and r.rule.region == region]
        matching = regional or [r for r in same if not r.rule.region]
        if amount is not None:
            matching = [
                r
                for r in matching
                if (r.rule.min_amount is None or r.rule.min_amount <= amount)
                and (r.rule.max_amount is None or amount <= r.rule.max_amount)
            ]
        return matching, actions

    def _resolve(self, phrase: str, requester: str, on: date) -> tuple[PersonView | None, list[PersonView], str]:
        with self._db.unit_of_work() as uow:
            d = DirectoryReader(uow)
            chain = d.reporting_chain(requester, on, self._cfg.max_chain_depth)
            grades = sorted(
                {str(r["grade"]) for r in uow.fetchall("SELECT DISTINCT grade FROM people WHERE grade IS NOT NULL")},
                key=natural_key,
            )
            words = [w for w in _WORD.findall(phrase) if w not in grades]
            named_grade = next(
                (g for g in sorted(grades, key=len, reverse=True) if re.search(rf"\b{re.escape(g)}\b", phrase)), None
            )

            def title_hit(p: PersonView) -> bool:
                if not p.title or not words:
                    return False
                tw = {w.lower() for w in _WORD.findall(p.title)}
                ac = acronym(p.title)
                return all(w.lower() in tw for w in words) or any(w == ac for w in words if w.isupper())

            for p in chain:
                if title_hit(p):
                    return p, chain, "title in reporting chain"
            if named_grade is not None:
                need = natural_key(named_grade)
                for p in chain:
                    if p.grade and natural_key(p.grade)[0] == need[0] and natural_key(p.grade)[1] >= need[1]:
                        return p, chain, f"first in reporting chain at {named_grade} or above"
            if words:
                rows = uow.fetchall(
                    "SELECT id FROM people WHERE status = 'active' AND id <> :me ORDER BY id", {"me": requester}
                )
                for r in rows:
                    cand = d.person(str(r["id"]))
                    if cand is not None and title_hit(cand):
                        return cand, chain, "title match in directory"
        return None, chain, "unresolved"

    # ---------------------------------------------------------------- answer
    @traced(step="knowledge.who_approves")
    def who_approves(self, actor: Actor, action: str, amount: float | None, on_date: date | None) -> ApproverAnswer:
        on = on_date or actor.asof
        with self._db.unit_of_work(as_person=actor.person_id, asof=on) as uow:
            me = DirectoryReader(uow).person(actor.person_id)
        region = me.country if me else None
        matching, actions = self._pick(self.rows(on, person_id=actor.person_id), action, region, amount)
        if len(matching) != 1:
            why = (
                "Several rows match; ask for the amount or which one applies."
                if matching
                else "No single matching rule; pick one of the candidate actions."
            )
            cands = [f"{r.rule.rule_id}: {r.rule.action} {r.rule.min_amount}-{r.rule.max_amount}" for r in matching]
            return ApproverAnswer(requester=actor.person_id, asof=on, rule=None, candidates=cands or actions, note=why)
        row = matching[0]
        approver, chain, how = self._resolve(row.rule.approver_phrase, actor.person_id, on)
        rule_cite = Citation(
            id=row.source_doc or row.record_id,
            kind="record",
            title=f"{row.rule.rule_id}: {row.rule.action}",
            quote=row.quote[:240],
            valid_from=row.rule.valid_from,
            authority="approved policy",
        )
        person_cite = (
            Citation(id=approver.id, kind="person", title=f"{approver.name}, {approver.title} ({approver.grade})")
            if approver
            else None
        )
        note = (
            f"Resolved by {how}."
            if approver
            else f"Approver is '{row.rule.approver_phrase}'; pick from the reporting chain or ask your manager."
        )
        cites = [rule_cite, *([person_cite] if person_cite else [])]
        did = self._audit.record(
            actor,
            tool="who_approves",
            question=f"{action} amount={amount} on={on}",
            answer=f"{row.rule.rule_id}: {approver.id if approver else row.rule.approver_phrase}",
            citations=cites,
            on_date=on,
        )
        dataflow(Hop.RETRIEVAL, kind="who_approves", rule=row.rule.rule_id, approver=approver.id if approver else None)
        return ApproverAnswer(
            requester=actor.person_id,
            asof=on,
            rule=row.rule,
            approver=person_cite,
            chain=[f"{p.id} {p.name}, {p.title} ({p.grade})" for p in chain],
            citations=cites,
            decision_id=did,
            note=note,
        )
