"""Decision audit: every company answer the brain gives is stored with its citations and projected as (:Decision)."""

from __future__ import annotations

import json
from datetime import date

from nwb.db.outbox import OutboxRepository
from nwb.db.repo_sessions import new_id
from nwb.identity import Actor
from nwb.knowledge.models import Citation
from nwb.obs.log import Hop, dataflow
from nwb.ports import SqlDatabase


class DecisionAuditor:
    def __init__(self, db: SqlDatabase) -> None:
        self._db = db

    def record(
        self, actor: Actor, *, tool: str, question: str, answer: str, citations: list[Citation], on_date: date
    ) -> str:
        did = new_id("dec")
        cited = [c.id for c in citations]
        with self._db.unit_of_work(as_person=actor.person_id, asof=actor.asof) as uow:
            uow.execute(
                "INSERT INTO decision_audits (id, company_id, person_id, session_id, run_id, asof, on_date, tool, "
                "question, answer, citations) VALUES (:id, :c, :p, :s, :r, :a, :d, :t, :q, :ans, :cit)",
                {
                    "id": did,
                    "c": actor.company_id,
                    "p": actor.person_id,
                    "s": actor.session_id,
                    "r": actor.run_id,
                    "a": actor.asof,
                    "d": on_date,
                    "t": tool,
                    "q": question[:500],
                    "ans": answer[:1000],
                    "cit": json.dumps(cited),
                },
            )
            OutboxRepository(uow).add(
                "decision",
                did,
                {"id": did, "person_id": actor.person_id, "tool": tool, "asof": str(actor.asof), "cites": cited},
            )
        dataflow(Hop.TOOL_CALL, kind="decision_audit", decision=did, tool=tool, cites=cited, content=answer)
        return did
