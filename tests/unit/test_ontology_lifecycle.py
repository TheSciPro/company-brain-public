"""Ontology approval is a hash of the proposal. Loads keep only names the approved file has.

No hosted model: discovery is a stand-in, and one offline ``Ontology.from_sources`` checks the SDK path.
"""

from __future__ import annotations

import asyncio
import re
import socket
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")

from graphrag_sdk import Attribute, Entity, Ontology, Relation
from graphrag_sdk.core.models import GraphData, GraphNode, GraphRelationship
from graphrag_sdk.ingestion.extraction_strategies.base import ExtractionStrategy

from nwb.actions.service import EXECUTORS, ActionService
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import (
    ActionsConfig,
    CompanyGraphConfig,
    GraphConfig,
    GraphLoadConfig,
    OntologyLifecycleConfig,
    ProseDecl,
    TableDecl,
)
from nwb.errors import ConflictError, NotFoundError
from nwb.kg.catalog import LevelBundle, TableSlice
from nwb.kg.load import ontology_for_level
from nwb.kg.offline_llm import OfflineSdkLLM
from nwb.kg.ontology import (
    ApprovedOnly,
    EntitySpec,
    OntologyLifecycle,
    OntologyProposal,
    declared_ontology,
    diff_ontologies,
    discover_from_sources,
    file_hash,
    render_proposal,
    restrict,
    save_ontology,
)
from tests.conftest import ROOT, fresh_db

ASOF = date(2026, 3, 1)
PERSON = "P-review"
GROUP = "grp-review"
_LOCAL = {"127.0.0.1", "localhost", "::1"}


def _actions() -> ActionsConfig:
    return ConfigLoader(ROOT / "config").load("actions", ActionsConfig)


def _graph(root: Path, *, tables: tuple[TableDecl, ...] = ()) -> GraphConfig:
    life = OntologyLifecycleConfig(
        file="approved.json",
        pending_file="pending.json",
        state_file="state.json",
        reviewer_role="executive",
        decision_type_pattern=r"(?i)decis",
        classification_properties=("classification", "cls"),
        action_kind="review_ontology",
    )
    load = GraphLoadConfig(boundaries="notes", tables=tables, prose=(ProseDecl(root="."),))
    return GraphConfig(version=1, company=CompanyGraphConfig(ontology=life, load=load))


def _seed(db: Any) -> None:
    with db.unit_of_work() as uow:
        uow.execute("INSERT INTO companies (id, name) VALUES ('co', 'Acme')")
        uow.execute(
            "INSERT INTO people (id, company_id, name, status) VALUES (:id, 'co', 'Reviewer', 'active')",
            {"id": PERSON},
        )
        uow.execute("INSERT INTO groups (id, company_id, name) VALUES (:id, 'co', 'Reviewers')", {"id": GROUP})
        uow.execute(
            "INSERT INTO group_members (group_id, person_id, valid_from) VALUES (:g, :p, :d)",
            {"g": GROUP, "p": PERSON, "d": ASOF},
        )


def _wire(
    db: Any, root: Path, discover: Any, cfg: GraphConfig | None = None
) -> tuple[OntologyLifecycle, ActionService]:
    holder: dict[str, OntologyLifecycle] = {}

    def apply(_uow: Any, action: Any) -> str:
        return holder["life"].apply_approved(action)

    actions = ActionService(db, _actions(), executors={**EXECUTORS, "apply_ontology": apply})
    life = OntologyLifecycle(db, actions, cfg or _graph(root), (GROUP,), root, discover=discover)
    holder["life"] = life
    return life, actions


def _note(root: Path, text: str) -> Path:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "note.md"
    path.write_text(text, encoding="utf-8")
    return data


def _person_only() -> Ontology:
    return Ontology(entities=[Entity(label="Person", description="A person.")], relations=[])


def test_shipped_review_kind_is_configured() -> None:
    kind = _actions().kinds["review_ontology"]
    assert kind.approver == "configured"
    assert kind.executor == "apply_ontology"


def test_shipped_ontology_matches_the_declared_catalog() -> None:
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    assert cfg.company.load is not None
    approved = Ontology.from_file(str(ROOT / cfg.company.ontology.file))
    declared = declared_ontology(cfg.company.load)
    assert {entity.label for entity in approved.entities} == {entity.label for entity in declared.entities}
    assert {(rel.label, tuple(rel.patterns)) for rel in approved.relations} == {
        (rel.label, tuple(rel.patterns)) for rel in declared.relations
    }


def test_restrict_drops_types_the_approved_ontology_does_not_name() -> None:
    candidate = Ontology(
        entities=[Entity(label="Person"), Entity(label="Decision")],
        relations=[
            Relation(label="KNOWS", patterns=[("Person", "Person")]),
            Relation(label="DECIDES", patterns=[("Person", "Decision")]),
        ],
    )
    approved = Ontology(
        entities=[Entity(label="Person")],
        relations=[Relation(label="KNOWS", patterns=[("Person", "Person")])],
    )
    kept = restrict(candidate, approved)
    assert {entity.label for entity in kept.entities} == {"Person"}
    assert {relation.label for relation in kept.relations} == {"KNOWS"}


def test_unapproved_table_label_does_not_load(tmp_path: Path) -> None:
    secret = TableDecl(
        path="hidden.csv",
        source_name="hidden.csv",
        label="Decision",
        key="id",
        name="id",
        links=(),
    )
    people = TableDecl(
        path="people.csv",
        source_name="people.csv",
        label="Person",
        key="id",
        name="id",
        links=(),
    )
    bundle = LevelBundle(
        level="internal",
        directory=tmp_path,
        tables=[
            TableSlice(secret.source_name, tmp_path / secret.source_name, 1, secret),
            TableSlice(people.source_name, tmp_path / people.source_name, 1, people),
        ],
    )
    approved = Ontology(entities=[Entity(label="Person")], relations=[])
    gated = ontology_for_level(approved, bundle)
    assert [table.label for table in gated.tables] == ["Person"]


def test_proposal_hash_ignores_the_sources_fingerprint() -> None:
    base = OntologyProposal(base_hash="abc", sources_hash="one", new_entities=(EntitySpec(label="Location"),))
    same = OntologyProposal(base_hash="abc", sources_hash="two", new_entities=(EntitySpec(label="Location"),))
    changed = OntologyProposal(base_hash="abc", sources_hash="one", new_entities=(EntitySpec(label="Place"),))
    assert base.digest() == same.digest()
    assert base.digest() != changed.digest()
    assert "Location" in render_proposal(base)


def test_unattended_refresh_files_a_sensitive_type_and_does_not_apply(tmp_path: Path) -> None:
    db = fresh_db("sqlite", tmp_path)
    _seed(db)
    save_ontology(_person_only(), tmp_path / "approved.json")
    before = file_hash(tmp_path / "approved.json")
    calls: list[int] = []

    async def discover(_paths: list[str], _llm: Any, **_kwargs: Any) -> Ontology:
        calls.append(1)
        return Ontology(entities=[Entity(label="Decision", description="A recorded decision.")])

    data = _note(tmp_path, "A recorded decision about a policy.\n")
    life, _ = _wire(db, tmp_path, discover)
    report = asyncio.run(life.refresh(data_dir=data, llm=None, asof=ASOF, unattended=True))
    assert report.filed and report.sensitive
    assert report.new_entities == ["Decision"]
    assert file_hash(tmp_path / "approved.json") == before
    with db.unit_of_work() as uow:
        row = uow.fetchone("SELECT kind, title, body FROM inbox_messages")
    assert row is not None
    assert row["kind"] == "approval"
    assert "Sensitive" in str(row["title"])
    assert "Decision" in str(row["body"])
    assert "sensitive decision_type" in str(row["body"])
    assert report.proposal_hash in str(row["body"])
    again = asyncio.run(life.refresh(data_dir=data, llm=None, asof=ASOF, unattended=True))
    assert again.skipped and len(calls) == 1
    _note(tmp_path, "A recorded decision about a different policy.\n")
    third = asyncio.run(life.refresh(data_dir=data, llm=None, asof=ASOF, unattended=True))
    assert not third.skipped and len(calls) == 2
    db.close()


def test_classification_change_is_sensitive_and_waits(tmp_path: Path) -> None:
    approved = Ontology(entities=[Entity(label="Person", properties=[Attribute(name="classification", type="STRING")])])
    discovered = Ontology(
        entities=[Entity(label="Person", properties=[Attribute(name="classification", type="INTEGER")])]
    )
    proposal = diff_ontologies(
        approved,
        discovered,
        decision_type=re.compile("(?i)decis"),
        classification_properties=frozenset({"classification", "cls"}),
        base_hash="b",
        sources_hash="s",
    )
    assert proposal.sensitive and proposal.sensitive[0].kind == "classification"
    assert proposal.attribute_changes[0].change == "retype"
    assert "sensitive classification" in render_proposal(proposal)


def test_changed_proposal_is_rejected_and_a_matching_one_is_applied(tmp_path: Path) -> None:
    db = fresh_db("sqlite", tmp_path)
    _seed(db)
    approved_path = tmp_path / "approved.json"
    save_ontology(_person_only(), approved_path)
    before = approved_path.read_bytes()

    async def discover(_paths: list[str], _llm: Any, **_kwargs: Any) -> Ontology:
        return Ontology(entities=[Entity(label="Location", description="A place.")])

    data = _note(tmp_path, "The team met in one city.\n")
    life, actions = _wire(db, tmp_path, discover)
    report = asyncio.run(life.refresh(data_dir=data, llm=None, asof=ASOF, unattended=True))
    assert report.proposal_hash and not report.sensitive
    pending = OntologyProposal.model_validate_json((tmp_path / "pending.json").read_text(encoding="utf-8"))
    tweaked = pending.model_copy(update={"new_entities": (*pending.new_entities, EntitySpec(label="Extra"))})
    (tmp_path / "pending.json").write_text(tweaked.model_dump_json(indent=2), encoding="utf-8")
    approval = actions.pending_for(PERSON)[0]
    assert approval.approval_id
    with pytest.raises(ConflictError, match="changed"):
        actions.decide(PERSON, approval.approval_id, "approve", None)
    assert approved_path.read_bytes() == before
    still = actions.pending_for(PERSON)
    assert still and still[0].status == "awaiting_approval"
    (tmp_path / "pending.json").write_text(
        OntologyProposal.model_validate(approval.args["proposal"]).model_dump_json(indent=2),
        encoding="utf-8",
    )
    done = actions.decide(PERSON, approval.approval_id, "approve", None)
    assert done.status == "done"
    labels = {entity.label for entity in Ontology.from_file(str(approved_path)).entities}
    assert labels == {"Person", "Location"}
    db.close()


def test_chat_cannot_choose_the_ontology_approver(tmp_path: Path) -> None:
    db = fresh_db("sqlite", tmp_path)
    _seed(db)
    actions = ActionService(db, _actions())
    with pytest.raises(NotFoundError, match="approver"):
        actions.propose(
            kind_name="review_ontology",
            requester=PERSON,
            target=None,
            title="Ontology change",
            args={"message": "add a type"},
            asof=ASOF,
            proposed_by="agent",
        )
    db.close()


def test_offline_from_sources_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    original = socket.socket.connect

    def _guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in _LOCAL:
            raise AssertionError(f"discovery opened a network connection to {address!r}")
        original(self, address)

    monkeypatch.setattr(socket.socket, "connect", _guard)
    note = tmp_path / "note.md"
    note.write_text("Priya Nair joined the Platform team.\n", encoding="utf-8")
    draft = asyncio.run(
        discover_from_sources(
            [str(note)],
            OfflineSdkLLM("offline-kg"),
            method="llm",
            boundaries="notes",
            sample_chunks_per_doc=1,
            concurrency=1,
            seed=0,
            existing=Ontology(),
        )
    )
    labels = {entity.label for entity in draft.entities}
    assert {"Person", "Organization"} <= labels
    assert "AFFILIATED_WITH" in {relation.label for relation in draft.relations}
    path = tmp_path / "ont.json"
    save_ontology(draft, path)
    restored = Ontology.from_file(str(path))
    assert {entity.label for entity in restored.entities} == labels


class _CannedExtraction(ExtractionStrategy):
    """What the SDK extractor hands back: an unapproved type arrives as ``Unknown``."""

    def __init__(self, data: GraphData) -> None:
        self._data = data

    async def extract(self, chunks: Any, ontology: Ontology, ctx: Any) -> GraphData:
        return self._data


def _relates(src: str, tgt: str, rel_type: str) -> GraphRelationship:
    return GraphRelationship(start_node_id=src, end_node_id=tgt, type="RELATES", properties={"rel_type": rel_type})


def test_extraction_drops_unknown_and_unapproved_labels_before_write() -> None:
    approved = Ontology(
        entities=[Entity(label="Person"), Entity(label="Organization")],
        relations=[Relation(label="WORKS_AT", patterns=[("Person", "Organization")]), Relation(label="KNOWS")],
    )
    canned = GraphData(
        nodes=[
            GraphNode(id="ana", label="Person"),
            GraphNode(id="acme", label="Organization"),
            GraphNode(id="blob", label="Unknown"),
            GraphNode(id="gizmo", label="Gadget"),
        ],
        relationships=[
            _relates("ana", "acme", "WORKS_AT"),
            _relates("ana", "acme", "KNOWS"),
            _relates("acme", "ana", "WORKS_AT"),
            _relates("ana", "acme", "OWNS"),
            _relates("ana", "blob", "KNOWS"),
            _relates("ana", "gizmo", "KNOWS"),
        ],
    )
    out = asyncio.run(ApprovedOnly(_CannedExtraction(canned), approved).extract(None, approved, None))
    assert sorted(node.label for node in out.nodes) == ["Organization", "Person"]
    assert [(r.start_node_id, r.end_node_id, r.properties["rel_type"]) for r in out.relationships] == [
        ("ana", "acme", "WORKS_AT"),
        ("ana", "acme", "KNOWS"),
    ]
