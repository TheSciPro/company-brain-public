"""Company knowledge tools: as-of facts (keys discovered from the data), history, documents, approvals, people.

Every answer is access-scoped (judged today), carries citations, and is recorded as a decision audit.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool, ToolException

from nwb.agent.tools.base import Result, context, domain_errors, finish, result
from nwb.db.repo_directory import DirectoryReader
from nwb.knowledge.approvals import ApprovalMatrix
from nwb.knowledge.models import PersonCard
from nwb.knowledge.search import CompanySearch
from nwb.knowledge.service import KnowledgeService
from nwb.ports import SqlDatabase


class KnowledgeTools:
    def __init__(
        self, db: SqlDatabase, knowledge: KnowledgeService, approvals: ApprovalMatrix, search: CompanySearch
    ) -> None:
        self._db = db
        self._kb = knowledge
        self._approvals = approvals
        self._search = search

    def tools(self) -> list[BaseTool]:
        kb, approvals, search, db = self._kb, self._approvals, self._search, self._db

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def company_fact(
            question: str, runtime: ToolRuntime[Any, Any], client: str | None = None, on_date: date | None = None
        ) -> Result:
            """Answer a question about a company rule, limit or number as of today, for THIS user.

            Call this once. If ``say`` is set, tell the user that sentence and stop. Do not call another tool.
            If ``say`` or ``note`` says the fact is not on record, say that and who to ask. Never answer from
            a different key, and never mention counts or hidden sources.

            Args:
                question: the user's question in their own words.
                client: a client name only if the user names one.
                on_date: only when the user asks about a different date (YYYY-MM-DD).
            """
            return result(kb.lookup(context(runtime).actor(), question, client, on_date))

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def fact_history(key: str, runtime: ToolRuntime[Any, Any]) -> Result:
            """Every version of one fact key this user may see.

            Call only after company_fact, and only when the user asks how a fact changed. If the result says
            the key is not on record, repeat that and stop. Do not mention counts.

            Args:
                key: a fact key returned by company_fact.
            """
            return result(kb.history(context(runtime).actor(), key))

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def search_company(
            query: str, runtime: ToolRuntime[Any, Any], kind: str | None = None, limit: int | None = None
        ) -> Result:
            """Search the company knowledge graphs this user may read, and return the passages retrieved.

            Only graphs for access levels this user can see in full are opened. ``not_searched`` names the
            levels that were not opened. Passages whose top score is under the configured evidence floor
            are not returned. Cite only the returned passages. If nothing is returned, ``say`` is the reply:
            give it and stop.

            Args:
                query: the question in the user's words.
                kind: ignored. Each allowed graph is searched as a whole.
                limit: max passages.
            """
            if len(query.strip()) < 2:
                raise ToolException("query too short")
            actor = context(runtime).actor()
            found = search.search(actor, query, kind, limit)
            if not found.hits:  # the same "not on record, ask X" as company_fact; never a hint of hidden sources
                found = found.model_copy(update={"say": kb.not_on_record(actor), "stop": True})
            return result(found)

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def who_approves(
            action: str, runtime: ToolRuntime[Any, Any], amount: float | None = None, on_date: date | None = None
        ) -> Result:
            """Who must approve something for this user: the matching delegation-of-authority rule and the person.

            Args:
                action: what needs approval, in the user's words.
                amount: the amount, number of days or percent as a number, if given.
                on_date: only when the user asks about a different date (YYYY-MM-DD).
            """
            return result(approvals.who_approves(context(runtime).actor(), action, amount, on_date))

        @tool(response_format="content_and_artifact", parse_docstring=True)
        @domain_errors
        def org_lookup(who: str, runtime: ToolRuntime[Any, Any]) -> Result:
            """Look up a colleague by id or name: title, grade, location, manager and units as of today.

            Args:
                who: a person id or a name.
            """
            asof = context(runtime).asof_date
            with db.unit_of_work() as uow:
                d = DirectoryReader(uow)
                person = (
                    d.person(who.strip()) or d.person(who.strip().upper()) or next(iter(d.find_people(who, 1)), None)
                )
                if person is None:
                    raise ToolException(f"no person matching {who!r}")
                mgr = d.manager(person.id, asof)
                units = [m.unit_name for m in d.memberships(person.id, asof)]
            return result(
                PersonCard(
                    id=person.id,
                    name=person.name,
                    title=person.title,
                    grade=person.grade,
                    location=person.location,
                    manager=f"{mgr.name} ({mgr.id})" if mgr else None,
                    units=units,
                    asof=asof,
                )
            )

        return [finish(t) for t in (company_fact, fact_history, search_company, who_approves, org_lookup)]
