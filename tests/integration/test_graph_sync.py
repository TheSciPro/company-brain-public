"""Outbox -> FalkorDBLite projection on real Layer-1 data."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nwb.graph.client import FalkorGraphStore, connect_lite
from nwb.graph.schema import ensure_schema
from nwb.jobs.graph_sync import GraphSync
from nwb.ports import SqlDatabase
from tests.conftest import fresh_db
from tests.integration.test_layer1 import loader

pytestmark = pytest.mark.graph


@pytest.fixture(scope="module")
def synced(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[SqlDatabase, FalkorGraphStore, Any]]:
    tmp: Path = tmp_path_factory.mktemp("g")
    db = fresh_db("sqlite", tmp)
    loader(db).load()
    graph, lite = connect_lite(tmp / "falkor.db", "nw_test")
    ensure_schema(graph)
    report = GraphSync(db, graph).drain()
    assert report.failed == 0 and report.pending == 0
    yield db, graph, lite
    db.close()


def one(graph: FalkorGraphStore, q: str) -> Any:
    return graph.ro_query(q)[0][0]


def test_counts_match_sql(synced):
    _, graph, _ = synced
    assert one(graph, "MATCH (p:Person) RETURN count(p)") == 3002
    assert one(graph, "MATCH (u:Unit)-[:PART_OF]->(:Unit {id:'ORG'}) RETURN count(u)") == 14
    assert (
        one(
            graph,
            "MATCH (:Person {id:'EXT-AA-01'})-[e:IN_GROUP]->(:Group {id:'grp-auditor-ext'}) RETURN toString(e.to)",
        )
        == "2026-10-30"
    )


def test_dated_membership_as_of(synced):
    _, graph, _ = synced
    q = (
        "MATCH (:Person {id:'NW0011'})-[m:MEMBER_OF]->(u:Unit) WHERE m.from <= date($d) "
        "AND (m.to IS NULL OR m.to >= date($d)) RETURN u.id"
    )
    assert graph.ro_query(q, {"d": "2026-04-01"}) == [["RTL"]]
    assert graph.ro_query(q, {"d": "2026-06-01"}) == [["CON"]]


def test_replay_is_idempotent(synced):
    db, graph, _ = synced
    before = one(graph, "MATCH ()-[r]->() RETURN count(r)")
    with db.unit_of_work() as uow:
        uow.execute("UPDATE graph_outbox SET processed_at = NULL")
    GraphSync(db, graph).drain()
    assert one(graph, "MATCH ()-[r]->() RETURN count(r)") == before


def test_failure_keeps_error_and_stops(tmp_path: Path):
    db = fresh_db("sqlite", tmp_path)
    with db.unit_of_work() as uow:
        uow.execute(
            "INSERT INTO graph_outbox (aggregate, aggregate_id, op, payload) VALUES ('mystery','x','upsert','{}')"
        )
    graph, _lite = connect_lite(tmp_path / "f.db", "nw_fail")
    rep = GraphSync(db, graph).run_once()
    assert (rep.processed, rep.failed, rep.pending) == (0, 1, 1)
    with db.unit_of_work() as uow:
        row = uow.fetchone("SELECT attempts, error FROM graph_outbox")
    assert row is not None and row["attempts"] == 1 and "no projector" in row["error"]
