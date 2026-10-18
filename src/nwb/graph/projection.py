"""Outbox aggregate -> Cypher. One Projector per aggregate (open/closed: add a class, register it).

All statements are batched with UNWIND and use MERGE, so replays are idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from nwb.ports import GraphStore

_DATE = "CASE WHEN {v} IS NULL THEN NULL ELSE date({v}) END"


class Projector:
    aggregate: ClassVar[str]
    cypher: ClassVar[str]

    def rows(self, payloads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(payloads)

    def apply(self, graph: GraphStore, payloads: Sequence[dict[str, Any]]) -> None:
        graph.query(self.cypher, {"rows": self.rows(payloads)})


class CompanyProjector(Projector):
    aggregate = "company"
    cypher = "UNWIND $rows AS r MERGE (c:Company {id: r.id}) SET c.name = r.name"


class UnitProjector(Projector):
    aggregate = "unit"
    cypher = (
        "UNWIND $rows AS r MERGE (u:Unit {id: r.id}) "
        "SET u.name = r.name, u.kind = r.kind, u.company_id = r.company_id, "
        f"u.valid_from = {_DATE.format(v='r.valid_from')}, u.valid_to = {_DATE.format(v='r.valid_to')} "
        "WITH u, r WHERE r.parent_id IS NOT NULL "
        "MERGE (p:Unit {id: r.parent_id}) MERGE (u)-[:PART_OF]->(p)"
    )


class PersonProjector(Projector):
    aggregate = "person"
    cypher = (
        "UNWIND $rows AS r MERGE (p:Person {id: r.id}) "
        "SET p.name = r.name, p.title = r.title, p.grade = r.grade, p.location = r.location, "
        "p.status = r.status, p.external = r.external, p.company_id = r.company_id, "
        f"p.hired_on = {_DATE.format(v='r.hired_on')}"
    )


class MembershipProjector(Projector):
    aggregate = "membership"
    cypher = (
        "UNWIND $rows AS r MERGE (p:Person {id: r.person_id}) MERGE (u:Unit {id: r.unit_id}) "
        "MERGE (p)-[m:MEMBER_OF {id: r.id}]->(u) "
        f"SET m.from = date(r.valid_from), m.to = {_DATE.format(v='r.valid_to')}, "
        "m.leadership = r.is_leader, m.primary = r.is_primary, m.role = r.role, m.src = r.src_id"
    )


class ReportingLineProjector(Projector):
    aggregate = "reporting_line"
    cypher = (
        "UNWIND $rows AS r MERGE (p:Person {id: r.person_id}) MERGE (m:Person {id: r.manager_id}) "
        "MERGE (p)-[e:REPORTS_TO {from: date(r.valid_from)}]->(m) "
        f"SET e.to = {_DATE.format(v='r.valid_to')}, e.src = r.src_id"
    )


class GroupProjector(Projector):
    aggregate = "group"
    cypher = "UNWIND $rows AS r MERGE (g:Group {id: r.id}) SET g.name = r.name"


class GroupMemberProjector(Projector):
    aggregate = "group_member"
    cypher = (
        "UNWIND $rows AS r MERGE (p:Person {id: r.person_id}) MERGE (g:Group {id: r.group_id}) "
        "MERGE (p)-[e:IN_GROUP {from: date(r.valid_from)}]->(g) "
        f"SET e.to = {_DATE.format(v='r.valid_to')}"
    )


class AccessRuleProjector(Projector):
    aggregate = "access_rule"
    cypher = (
        "UNWIND $rows AS r MERGE (t:AccessTag {id: r.source_tag}) "
        "SET t.cls = r.classification, t.share = r.share_scope "
        "WITH t, r UNWIND r.groups AS gid MERGE (g:Group {id: gid}) MERGE (t)-[:GRANTS_TO]->(g)"
    )
    person_cypher = (
        "UNWIND $rows AS r MATCH (t:AccessTag {id: r.source_tag}) "
        "UNWIND r.persons AS pid MERGE (p:Person {id: pid}) MERGE (t)-[:GRANTS_TO]->(p)"
    )

    def rows(self, payloads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **p,
                "groups": [s for t, s in p.get("subjects", []) if t == "group"],
                "persons": [s for t, s in p.get("subjects", []) if t == "person"],
            }
            for p in payloads
        ]

    def apply(self, graph: GraphStore, payloads: Sequence[dict[str, Any]]) -> None:
        rows = self.rows(payloads)
        graph.query(self.cypher, {"rows": rows})
        graph.query(self.person_cypher, {"rows": rows})


RECORD_LABELS: dict[str, str] = {
    "approval_records": "Approval",
    "background_approvals": "Approval",
    "pricing_exceptions": "Approval",
    "calendar_events": "Meeting",
    "slack_threads": "Thread",
    "escalations": "Escalation",
    "obligations": "Obligation",
    "activity_events": "Activity",
    "codeowners_history": "Ownership",
    "commits": "Commit",
    "file_history_runbook": "FileHistory",
    "jira_issues": "Ticket",
    "pull_requests": "PullRequest",
    "repositories": "Repo",
    "hr_cases": "Ticket",
    "skills_certs": "Skillset",
    "access_review_schedule": "AccessReview",
    "audit_findings": "Finding",
    "controls": "Control",
    "evidence": "Evidence",
    "incidents": "Incident",
    "intune_devices": "Device",
    "itsm_tickets": "Ticket",
    "okta_system_log": "LogEvent",
    "training_records": "Training",
    "allocations": "Allocation",
    "demands_and_bench_pulls": "Request",
    "milestones": "Milestone",
    "delegation_of_authority_matrix": "DoaRule",
}
DOC_LABELS: dict[str, str] = {
    "policies": "Policy",
    "contracts": "Contract",
    "decisions": "DecisionRecord",
    "collab/minutes": "Minutes",
    "hris": "Announcement",
}
_CONTENT_SET = (
    "SET n.company_id = r.company_id, n.cls = r.classification, n.share = r.share_scope, n.tag = r.source_tag, "
    "n.personal = r.personal, n.hash = r.hash, n.source = r.source "
)
_OWNERSHIP = (
    "WITH n, r OPTIONAL MATCH (u:Unit {id: r.owner_unit_id}) FOREACH (_ IN CASE WHEN u IS NULL THEN [] ELSE [1] END | "
    "MERGE (n)-[:OWNED_BY]->(u)) WITH n, r OPTIONAL MATCH (p:Person {id: r.owner_person_id}) "
    "FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | MERGE (n)-[:OWNED_BY_PERSON]->(p))"
)


class _LabelledProjector(Projector):
    """Groups rows by a whitelisted extra label (labels can't be query parameters)."""

    base: ClassVar[str]
    labels: ClassVar[dict[str, str]]
    label_field: ClassVar[str]
    extra_set: ClassVar[str] = ""

    def apply(self, graph: GraphStore, payloads: Sequence[dict[str, Any]]) -> None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for p in payloads:
            groups.setdefault(self.labels.get(str(p.get(self.label_field)), "Item"), []).append(p)
        for label, rows in groups.items():
            graph.query(
                f"UNWIND $rows AS r MERGE (n:{self.base} {{id: r.id}}) SET n:{label} "
                + _CONTENT_SET
                + self.extra_set
                + _OWNERSHIP,
                {"rows": rows},
            )


class RecordProjector(_LabelledProjector):
    aggregate = "record"
    base = "Record"
    labels = RECORD_LABELS
    label_field = "record_type"
    extra_set = ", n.type = r.record_type, n.source_id = r.source_id, n.occurred_at = r.occurred_at "


class DocumentProjector(_LabelledProjector):
    aggregate = "document"
    base = "Document"
    labels = DOC_LABELS
    label_field = "source"
    extra_set = ", n.title = r.title, n.doc_type = r.doc_type, n.version = r.version, n.blob_url = r.blob_url "


class GrantProjector(Projector):
    aggregate = "grant"

    def apply(self, graph: GraphStore, payloads: Sequence[dict[str, Any]]) -> None:
        for subj, slabel in (("group", "Group"), ("person", "Person"), ("unit", "Unit")):
            for rtype, rlabel in (("record", "Record"), ("document", "Document")):
                rows = [p for p in payloads if p["subject_type"] == subj and p["resource_type"] == rtype]
                if rows:
                    graph.query(
                        f"UNWIND $rows AS r MERGE (s:{slabel} {{id: r.subject_id}}) "
                        f"MERGE (n:{rlabel} {{id: r.resource_id}}) MERGE (s)-[g:CAN_READ {{id: r.id}}]->(n) "
                        f"SET g.from = date(r.valid_from), g.until = {_DATE.format(v='r.valid_to')}, "
                        "g.relation = r.relation",
                        {"rows": rows},
                    )


def about_label(about: str, company_id: str = "NW") -> str:
    if about == company_id:
        return "Company"
    if about.startswith("ACC-") or about.startswith("CL-"):
        return "Client"
    if about.startswith("POL-"):
        return "PolicyFamily"
    return "Topic"


# Company facts are written by ``GraphFactBook`` into the per-level graphs, not through this outbox.
# There is no fact or alert projector: those rows are not produced.


class DecisionProjector(Projector):
    """(:Decision) audit node: who asked, when, which facts it cited. Edges attach only to existing nodes."""

    aggregate = "decision"
    _NODE = (
        "UNWIND $rows AS r MERGE (d:Decision {id: r.id}) SET d.tool = r.tool, d.asof = date(r.asof), "
        "d.owner = r.person_id WITH d, r MATCH (p:Person {id: r.person_id}) MERGE (d)-[:ASKED_BY]->(p)"
    )
    _CITES = (
        "UNWIND $rows AS r MATCH (d:Decision {id: r.id}) UNWIND r.cites AS src "
        "MATCH (n) WHERE (n:Document OR n:Record OR n:Person) AND n.id = src MERGE (d)-[:CITES]->(n)"
    )

    def apply(self, graph: GraphStore, payloads: Sequence[dict[str, Any]]) -> None:
        rows = list(payloads)
        graph.query(self._NODE, {"rows": rows})
        graph.query(self._CITES, {"rows": rows})


DEFAULT_PROJECTORS: tuple[Projector, ...] = (
    CompanyProjector(),
    UnitProjector(),
    PersonProjector(),
    MembershipProjector(),
    ReportingLineProjector(),
    GroupProjector(),
    GroupMemberProjector(),
    AccessRuleProjector(),
    RecordProjector(),
    DocumentProjector(),
    GrantProjector(),
    DecisionProjector(),
)
