"""Graph indexes for Layer 1 labels. Idempotent: 'already indexed' errors are ignored."""

from __future__ import annotations

from nwb.ports import GraphStore

LABELS = (
    "Company",
    "Unit",
    "Person",
    "Group",
    "AccessTag",
    "Record",
    "Document",
    "Fact",
    "Client",
    "Topic",
    "PolicyFamily",
    "Alert",
    "Decision",
)
EDGE_INDEXES = (("MEMBER_OF", "from"), ("IN_GROUP", "from"), ("REPORTS_TO", "from"), ("CAN_READ", "from"))


def ensure_schema(graph: GraphStore) -> int:
    created = 0
    stmts = [f"CREATE INDEX FOR (n:{lbl}) ON (n.id)" for lbl in LABELS] + [
        f"CREATE INDEX FOR ()-[r:{rel}]-() ON (r.{prop})" for rel, prop in EDGE_INDEXES
    ]
    for stmt in stmts:
        try:
            graph.query(stmt)
            created += 1
        except Exception as exc:  # FalkorDB raises ResponseError when the index exists
            if "already indexed" not in str(exc).lower():
                raise
    return created
