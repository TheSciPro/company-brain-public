"""Ontology lifecycle: discover, propose, approve by hash, load only what was approved.

GraphRAG-SDK ``Ontology.from_sources`` drafts types from the current sources.
The diff against the approved file is a proposal in the approvals inbox. The
approval stores the proposal hash (Helix): confirming a proposal that changed
after review is rejected. A load receives only labels the approved ontology
names, so an unattended run can add data of those types and nothing else.
Sensitive changes (a new decision type, a classification change) stay in the
inbox until a person approves that exact hash.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from graphrag_sdk import Attribute, Entity, Ontology, Relation
from graphrag_sdk.core.context import Context
from graphrag_sdk.core.models import GraphData, TextChunks
from graphrag_sdk.core.providers.base import LLMInterface
from graphrag_sdk.ingestion.extraction_strategies.base import ExtractionStrategy
from pydantic import BaseModel, ConfigDict

from nwb.actions.models import ActionView
from nwb.actions.service import ActionService
from nwb.conf.models import GraphConfig, GraphLoadConfig
from nwb.errors import ConfigError, ConflictError, DataLoadError, NotFoundError
from nwb.ingest.acl import classification_config
from nwb.kg.catalog import materialise
from nwb.obs.log import Hop, dataflow, get_logger, log_step

log = get_logger(__name__)

# The SDK writes ``name`` on every entity. It is not a schema change a person reviews.
_IGNORED_ATTRIBUTES = frozenset({"name"})
DiscoverFn = Callable[..., Awaitable[Ontology]]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AttrSpec(_Spec):
    name: str
    type: str
    description: str | None = None


class EntitySpec(_Spec):
    label: str
    description: str | None = None
    properties: tuple[AttrSpec, ...] = ()


class RelationSpec(_Spec):
    label: str
    description: str | None = None
    patterns: tuple[tuple[str, str], ...] = ()
    properties: tuple[AttrSpec, ...] = ()


class PatternSpec(_Spec):
    relation: str
    source: str
    target: str


class AttrChange(_Spec):
    owner: str
    name: str
    type: str
    description: str | None = None
    change: Literal["add", "retype", "drop"]


class SensitiveMark(_Spec):
    kind: Literal["decision_type", "classification"]
    label: str
    detail: str


class OntologyProposal(_Spec):
    """The exact change a person is asked to approve. The hash covers this payload."""

    base_hash: str
    sources_hash: str
    new_entities: tuple[EntitySpec, ...] = ()
    new_relations: tuple[RelationSpec, ...] = ()
    new_patterns: tuple[PatternSpec, ...] = ()
    attribute_changes: tuple[AttrChange, ...] = ()
    sensitive: tuple[SensitiveMark, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.new_entities or self.new_relations or self.new_patterns or self.attribute_changes or self.sensitive
        )

    def digest(self) -> str:
        """Hash of the schema change. ``sources_hash`` is evidence, not part of what was approved."""
        payload = self.model_dump(mode="json", exclude={"sources_hash"})
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class RefreshReport:
    sources_hash: str
    proposal_hash: str | None = None
    skipped: bool = False
    sensitive: bool = False
    filed: bool = False
    unattended: bool = True
    new_entities: list[str] = field(default_factory=list)
    new_relations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources_hash": self.sources_hash,
            "proposal_hash": self.proposal_hash,
            "skipped": self.skipped,
            "sensitive": self.sensitive,
            "filed": self.filed,
            "unattended": self.unattended,
            "new_entities": self.new_entities,
            "new_relations": self.new_relations,
        }


def file_hash(path: Path) -> str:
    """Hash of the approved file bytes. A missing file hashes as empty."""
    raw = path.read_bytes() if path.is_file() else b""
    return hashlib.sha256(raw).hexdigest()


def sources_fingerprint(prose: list[tuple[str, str]], load: GraphLoadConfig) -> str:
    """Hash of prose bodies plus the declared table schema. A source edit changes it."""
    documents = [{"id": doc_id, "sha": hashlib.sha256(text.encode()).hexdigest()} for doc_id, text in sorted(prose)]
    tables = [item.model_dump(mode="json") for item in load.tables]
    blob = json.dumps({"prose": documents, "tables": tables}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def declared_ontology(load: GraphLoadConfig) -> Ontology:
    """Entity and relation labels the table catalog already names. No model call."""
    labels: list[str] = []
    relations: dict[str, list[tuple[str, str]]] = {}
    for table in load.tables:
        labels.append(table.label)
        for link in table.links:
            labels.append(link.to)
            relations.setdefault(link.type, [])
            pattern = (table.label, link.to)
            if pattern not in relations[link.type]:
                relations[link.type].append(pattern)
    entities = [
        Entity(label=label, description="Declared by the source catalog.") for label in sorted(dict.fromkeys(labels))
    ]
    rels = [
        Relation(
            label=label,
            description="Declared by the source catalog.",
            patterns=sorted(patterns),
        )
        for label, patterns in sorted(relations.items())
    ]
    return Ontology(entities=entities, relations=rels)


class ApprovedOnly(ExtractionStrategy):
    """Prose extraction that never hands the pipeline a label the approved ontology does not name.

    The SDK extractor labels an unapproved or low-confidence type ``Unknown`` and the pipeline's own prune
    keeps ``Unknown``. This drops those nodes, and any edge whose type (or source/target pair, when the
    relation declares patterns) is not approved, before anything is written.
    """

    def __init__(self, inner: ExtractionStrategy, approved: Ontology) -> None:
        self._inner = inner
        self._entities = {entity.label for entity in approved.entities}
        self._relations = {relation.label: set(relation.patterns) for relation in approved.relations}

    async def extract(self, chunks: TextChunks, ontology: Ontology, ctx: Context) -> GraphData:
        data = await self._inner.extract(chunks, ontology, ctx)
        nodes = [node for node in data.nodes if node.label in self._entities]
        kept = {node.id: node.label for node in nodes}
        edges = []
        for edge in data.relationships:
            label = str(edge.properties.get("rel_type", edge.type))
            ends = (kept.get(edge.start_node_id), kept.get(edge.end_node_id))
            patterns = self._relations.get(label)
            if patterns is None or None in ends or (patterns and ends not in patterns):
                continue
            edges.append(edge)
        dropped_nodes, dropped_edges = len(data.nodes) - len(nodes), len(data.relationships) - len(edges)
        if dropped_nodes or dropped_edges:
            log.info("dropped unapproved extraction labels", nodes=dropped_nodes, edges=dropped_edges)
        return data.model_copy(update={"nodes": nodes, "relationships": edges})


def restrict(candidate: Ontology, approved: Ontology) -> Ontology:
    """Drop every type and relation the approved ontology does not name.

    Patterns that point at a dropped type are dropped with them. Table mappings
    whose row label is not approved are dropped, and so are their links.
    """
    allowed_entities = {entity.label for entity in approved.entities}
    allowed_relations = {relation.label for relation in approved.relations}
    entities = [entity for entity in candidate.entities if entity.label in allowed_entities]
    relations: list[Relation] = []
    for relation in candidate.relations:
        if relation.label not in allowed_relations:
            continue
        patterns = [(src, tgt) for src, tgt in relation.patterns if src in allowed_entities and tgt in allowed_entities]
        relations.append(
            Relation(
                label=relation.label,
                description=relation.description,
                patterns=patterns,
                properties=list(relation.properties),
            )
        )
    tables = []
    for mapping in candidate.tables:
        if mapping.label not in allowed_entities:
            continue
        links = [link for link in mapping.links if link.type in allowed_relations and link.to in allowed_entities]
        tables.append(
            type(mapping)(
                source=mapping.source,
                label=mapping.label,
                key=mapping.key,
                name=mapping.name,
                properties=dict(mapping.properties),
                links=links,
                standalone=mapping.standalone and not links,
                derived=mapping.derived,
                description=mapping.description,
            )
        )
    return Ontology(entities=entities, relations=relations, tables=tables)


def diff_ontologies(
    approved: Ontology,
    discovered: Ontology,
    *,
    decision_type: re.Pattern[str],
    classification_properties: frozenset[str],
    base_hash: str,
    sources_hash: str,
) -> OntologyProposal:
    """Additions and classification edits. Deleting a type is not proposed: absence is not evidence."""
    approved_entities = {entity.label: entity for entity in approved.entities}
    approved_relations = {relation.label: relation for relation in approved.relations}
    new_entities: list[EntitySpec] = []
    new_relations: list[RelationSpec] = []
    new_patterns: list[PatternSpec] = []
    changes: list[AttrChange] = []
    sensitive: list[SensitiveMark] = []

    for entity in sorted(discovered.entities, key=lambda item: item.label):
        current = approved_entities.get(entity.label)
        if current is None:
            new_entities.append(_entity_spec(entity))
            if decision_type.search(entity.label):
                sensitive.append(SensitiveMark(kind="decision_type", label=entity.label, detail="new type"))
            continue
        changes.extend(
            _attr_diff(entity.label, current.properties, entity.properties, classification_properties, sensitive)
        )

    for relation in sorted(discovered.relations, key=lambda item: item.label):
        current = approved_relations.get(relation.label)
        if current is None:
            new_relations.append(_relation_spec(relation))
            if decision_type.search(relation.label):
                sensitive.append(SensitiveMark(kind="decision_type", label=relation.label, detail="new relation"))
            continue
        known = set(current.patterns)
        for src, tgt in relation.patterns:
            if (src, tgt) not in known:
                new_patterns.append(PatternSpec(relation=relation.label, source=src, target=tgt))
        changes.extend(
            _attr_diff(relation.label, current.properties, relation.properties, classification_properties, sensitive)
        )

    return OntologyProposal(
        base_hash=base_hash,
        sources_hash=sources_hash,
        new_entities=tuple(new_entities),
        new_relations=tuple(new_relations),
        new_patterns=tuple(sorted(new_patterns, key=lambda item: (item.relation, item.source, item.target))),
        attribute_changes=tuple(sorted(changes, key=lambda item: (item.owner, item.name, item.change))),
        sensitive=tuple(sorted(sensitive, key=lambda item: (item.kind, item.label, item.detail))),
    )


def render_proposal(proposal: OntologyProposal) -> str:
    """Inbox text. The hash is what the approver is agreeing to."""
    lines = [f"proposal {proposal.digest()}"]
    sensitive_labels = {mark.label for mark in proposal.sensitive if mark.kind == "decision_type"}
    for entity in proposal.new_entities:
        suffix = " sensitive" if entity.label in sensitive_labels else ""
        lines.append(f"new type {entity.label}{suffix}")
    for relation in proposal.new_relations:
        suffix = " sensitive" if relation.label in sensitive_labels else ""
        lines.append(f"new relation {relation.label}{suffix}")
    for pattern in proposal.new_patterns:
        lines.append(f"new pattern {pattern.relation} {pattern.source} -> {pattern.target}")
    for change in proposal.attribute_changes:
        lines.append(f"attribute {change.owner}.{change.name} {change.change} {change.type}")
    for mark in proposal.sensitive:
        lines.append(f"sensitive {mark.kind} {mark.label} {mark.detail}")
    lines.append("Unattended loads add only types this ontology already names.")
    lines.append("Approval is tied to this proposal hash. A change after review is rejected.")
    return "\n".join(lines)


def apply_proposal(approved: Ontology, proposal: OntologyProposal) -> Ontology:
    """Merge an approved proposal onto the current ontology. Callers check the hash first."""
    extra = Ontology(
        entities=[_entity(spec) for spec in proposal.new_entities],
        relations=[_relation(spec) for spec in proposal.new_relations],
    )
    merged = approved.merge(extra)
    for pattern in proposal.new_patterns:
        merged = merged.merge(
            Ontology(
                relations=[
                    Relation(label=pattern.relation, patterns=[(pattern.source, pattern.target)]),
                ]
            )
        )
    for change in proposal.attribute_changes:
        merged = _apply_attr(merged, change)
    return merged


def save_ontology(ontology: Ontology, path: Path) -> None:
    """Write the SDK ontology JSON atomically. ``save_to_file`` is the SDK's own writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    ontology.save_to_file(str(temporary))
    temporary.replace(path)


async def discover_from_sources(
    paths: list[str],
    llm: LLMInterface,
    *,
    method: str,
    boundaries: str,
    sample_chunks_per_doc: int,
    concurrency: int,
    seed: int,
    existing: Ontology,
) -> Ontology:
    """Draft an ontology with ``Ontology.from_sources``. Nothing here is approved."""
    with log_step("kg.ontology.discover"):
        draft = await Ontology.from_sources(
            paths,
            llm,
            method=method,  # type: ignore[arg-type]
            boundaries=boundaries,
            existing=existing,
            sample_chunks_per_doc=sample_chunks_per_doc,
            concurrency=concurrency,
            seed=seed,
        )
        dataflow(
            Hop.INGEST,
            stage="ontology_discover",
            sources=len(paths),
            entities=len(draft.entities),
            relations=len(draft.relations),
        )
        log.info(
            "ontology discovered",
            sources=len(paths),
            entities=len(draft.entities),
            relations=len(draft.relations),
        )
        return draft


class OntologyLifecycle:
    """Discover when sources change, file the diff, and apply it only on a matching hash."""

    def __init__(
        self,
        db: Any,
        actions: ActionService,
        graph: GraphConfig,
        reviewer_groups: tuple[str, ...],
        root: Path,
        *,
        discover: DiscoverFn | None = None,
    ) -> None:
        self._db = db
        self._actions = actions
        self._graph = graph
        self._groups = reviewer_groups
        self._root = root
        self._discover = discover or discover_from_sources
        self._discover_injected = discover is not None
        life = graph.company.ontology
        try:
            self._decision = re.compile(life.decision_type_pattern)
        except re.error as exc:
            raise ConfigError(f"ontology.decision_type_pattern is not a regular expression: {exc}") from exc
        self._classification = frozenset(life.classification_properties)
        self._approved = root / life.file
        self._pending = root / life.pending_file
        self._state = root / life.state_file

    def load_approved(self) -> Ontology:
        if not self._approved.is_file():
            raise DataLoadError(f"approved ontology is missing: {self._approved}")
        return Ontology.from_file(str(self._approved))

    def reviewer(self, asof: date) -> str:
        """One active member of the configured reviewer group, stable by person id."""
        if not self._groups:
            raise NotFoundError("no reviewer group is configured for ontology changes")
        marks = ", ".join(f":g{i}" for i in range(len(self._groups)))
        params: dict[str, Any] = {f"g{i}": group for i, group in enumerate(self._groups)}
        params["asof"] = asof
        with self._db.unit_of_work() as uow:
            row = uow.fetchone(
                "SELECT gm.person_id FROM group_members gm JOIN people p ON p.id = gm.person_id "
                f"WHERE gm.group_id IN ({marks}) AND p.status = 'active' "
                "AND gm.valid_from <= :asof AND (gm.valid_to IS NULL OR gm.valid_to >= :asof) "
                "ORDER BY gm.person_id LIMIT 1",
                params,
            )
        if row is None:
            raise NotFoundError("no active member of the ontology reviewer group")
        return str(row["person_id"])

    async def refresh(
        self,
        *,
        data_dir: Path,
        llm: LLMInterface | None,
        asof: date,
        unattended: bool = True,
        force: bool = False,
    ) -> RefreshReport:
        """Re-run discovery when sources change and file the diff. Does not change the approved file.

        An unattended run may only load types the approved ontology already names.
        New types, and every sensitive change, wait in the inbox.
        """
        load = self._graph.company.load
        if load is None:
            raise DataLoadError("graph.yaml company.load is required")
        with log_step("kg.ontology.refresh"):
            with tempfile.TemporaryDirectory() as tmp:
                levels = tuple(level.code for level in classification_config().levels)
                bundles = materialise(data_dir, load, Path(tmp), levels)
                prose = [(doc.document_id, doc.text) for bundle in bundles for doc in bundle.prose]
                paths = [str(doc.path) for bundle in bundles for doc in bundle.prose]
                fingerprint = sources_fingerprint(prose, load)
                if not force and self._stored_sources() == fingerprint:
                    log.info("ontology sources unchanged", sources_hash=fingerprint)
                    return RefreshReport(sources_hash=fingerprint, skipped=True, unattended=unattended)
                approved = self.load_approved() if self._approved.is_file() else Ontology()
                draft = await self._draft(paths, llm, load, approved)
            combined = declared_ontology(load).merge(draft)
            proposal = diff_ontologies(
                approved,
                combined,
                decision_type=self._decision,
                classification_properties=self._classification,
                base_hash=file_hash(self._approved),
                sources_hash=fingerprint,
            )
            report = RefreshReport(
                sources_hash=fingerprint,
                proposal_hash=None if proposal.is_empty() else proposal.digest(),
                sensitive=bool(proposal.sensitive),
                unattended=unattended,
                new_entities=[entity.label for entity in proposal.new_entities],
                new_relations=[relation.label for relation in proposal.new_relations],
            )
            self._write_state(fingerprint)
            if proposal.is_empty():
                self._pending.unlink(missing_ok=True)
                log.info("ontology discovery matched the approved schema", sources_hash=fingerprint)
            else:
                self._write_pending(proposal)
                report.filed = self._file(proposal, asof)
                log.info(
                    "ontology proposal waiting",
                    proposal_hash=proposal.digest(),
                    new_entities=report.new_entities,
                    new_relations=report.new_relations,
                    sensitive=report.sensitive,
                    unattended=unattended,
                )
            dataflow(
                Hop.INGEST,
                stage="ontology",
                sources_hash=fingerprint,
                proposal_hash=report.proposal_hash,
                filed=report.filed,
                sensitive=report.sensitive,
                unattended=unattended,
                new_entities=len(report.new_entities),
                new_relations=len(report.new_relations),
            )
            return report

    def apply_approved(self, action: ActionView) -> str:
        """Apply the proposal the approver saw. A mismatch with the pending file is a conflict."""
        stored = str(action.args.get("proposal_hash") or "")
        proposal = OntologyProposal.model_validate(action.args.get("proposal") or {})
        pending = self._read_pending()
        if proposal.digest() != stored or pending is None or pending.digest() != stored:
            raise ConflictError("The suggested ontology changed. Review it again.")
        if file_hash(self._approved) != proposal.base_hash:
            raise ConflictError("The suggested ontology changed. Review it again.")
        approved = self.load_approved() if self._approved.is_file() else Ontology()
        merged = apply_proposal(approved, proposal)
        save_ontology(merged, self._approved)
        self._pending.unlink(missing_ok=True)
        log.info(
            "ontology approved",
            proposal_hash=stored,
            entities=len(merged.entities),
            relations=len(merged.relations),
        )
        dataflow(Hop.INGEST, stage="ontology_approved", proposal_hash=stored, entities=len(merged.entities))
        return f"approved ontology {stored[:12]}"

    async def _draft(
        self, paths: list[str], llm: LLMInterface | None, load: GraphLoadConfig, approved: Ontology
    ) -> Ontology:
        if not paths:
            return Ontology()
        if llm is None and not self._discover_injected:
            raise DataLoadError("ontology discovery needs an LLM; set LLM_GRAPH or pass a model")
        prior = Ontology(entities=list(approved.entities), relations=list(approved.relations))
        return await self._discover(
            paths,
            llm,
            method=load.discover_method,
            boundaries=load.boundaries,
            sample_chunks_per_doc=load.sample_chunks_per_doc,
            concurrency=self._graph.company.max_concurrency,
            seed=load.seed,
            existing=prior,
        )

    def _file(self, proposal: OntologyProposal, asof: date) -> bool:
        reviewer = self.reviewer(asof)
        title = "Sensitive ontology change" if proposal.sensitive else "Ontology change"
        view = self._actions.propose(
            kind_name=self._graph.company.ontology.action_kind,
            requester=reviewer,
            target=None,
            title=title,
            args={
                "message": render_proposal(proposal),
                "proposal_hash": proposal.digest(),
                "proposal": proposal.model_dump(mode="json"),
            },
            asof=asof,
            proposed_by="ontology",
            dedupe_key=f"ontology:{proposal.digest()}",
            approver_id=reviewer,
        )
        return view.note != "already proposed"

    def _write_pending(self, proposal: OntologyProposal) -> None:
        self._pending.parent.mkdir(parents=True, exist_ok=True)
        self._pending.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")

    def _read_pending(self) -> OntologyProposal | None:
        if not self._pending.is_file():
            return None
        return OntologyProposal.model_validate_json(self._pending.read_text(encoding="utf-8"))

    def _stored_sources(self) -> str:
        if not self._state.is_file():
            return ""
        raw = json.loads(self._state.read_text(encoding="utf-8"))
        return str(raw.get("sources_hash") or "")

    def _write_state(self, sources_hash: str) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        self._state.write_text(json.dumps({"sources_hash": sources_hash}), encoding="utf-8")


def _entity_spec(entity: Entity) -> EntitySpec:
    return EntitySpec(
        label=entity.label,
        description=entity.description,
        properties=tuple(_attr_spec(prop) for prop in entity.properties if prop.name not in _IGNORED_ATTRIBUTES),
    )


def _relation_spec(relation: Relation) -> RelationSpec:
    return RelationSpec(
        label=relation.label,
        description=relation.description,
        patterns=tuple(sorted((src, tgt) for src, tgt in relation.patterns)),
        properties=tuple(_attr_spec(prop) for prop in relation.properties if prop.name not in _IGNORED_ATTRIBUTES),
    )


def _attr_spec(prop: Attribute) -> AttrSpec:
    return AttrSpec(name=prop.name, type=prop.type, description=prop.description)


def _attr_diff(
    owner: str,
    current: list[Attribute],
    incoming: list[Attribute],
    classification_properties: frozenset[str],
    sensitive: list[SensitiveMark],
) -> list[AttrChange]:
    current_by_name = {prop.name: prop for prop in current if prop.name not in _IGNORED_ATTRIBUTES}
    incoming_by_name = {prop.name: prop for prop in incoming if prop.name not in _IGNORED_ATTRIBUTES}
    changes: list[AttrChange] = []
    for name, prop in sorted(incoming_by_name.items()):
        old = current_by_name.get(name)
        if old is None:
            changes.append(
                AttrChange(owner=owner, name=name, type=prop.type, description=prop.description, change="add")
            )
        elif old.type != prop.type:
            changes.append(
                AttrChange(owner=owner, name=name, type=prop.type, description=prop.description, change="retype")
            )
        else:
            continue
        if name in classification_properties:
            sensitive.append(SensitiveMark(kind="classification", label=owner, detail=f"{name} {changes[-1].change}"))
    for name, prop in sorted(current_by_name.items()):
        if name in incoming_by_name:
            continue
        changes.append(AttrChange(owner=owner, name=name, type=prop.type, description=prop.description, change="drop"))
        if name in classification_properties:
            sensitive.append(SensitiveMark(kind="classification", label=owner, detail=f"{name} drop"))
    return changes


def _entity(spec: EntitySpec) -> Entity:
    return Entity(
        label=spec.label,
        description=spec.description,
        properties=[
            Attribute(name=prop.name, type=prop.type, description=prop.description) for prop in spec.properties
        ],
    )


def _relation(spec: RelationSpec) -> Relation:
    return Relation(
        label=spec.label,
        description=spec.description,
        patterns=list(spec.patterns),
        properties=[
            Attribute(name=prop.name, type=prop.type, description=prop.description) for prop in spec.properties
        ],
    )


def _apply_attr(ontology: Ontology, change: AttrChange) -> Ontology:
    if change.change == "drop":
        entities = [_without(entity, change) for entity in ontology.entities]
        relations = [_without_rel(relation, change) for relation in ontology.relations]
        return ontology.model_copy(update={"entities": entities, "relations": relations})
    prop = Attribute(name=change.name, type=change.type, description=change.description)
    if any(entity.label == change.owner for entity in ontology.entities):
        return ontology.merge(Ontology(entities=[Entity(label=change.owner, properties=[prop])]))
    return ontology.merge(Ontology(relations=[Relation(label=change.owner, properties=[prop])]))


def _without(entity: Entity, change: AttrChange) -> Entity:
    if entity.label != change.owner:
        return entity
    return Entity(
        label=entity.label,
        description=entity.description,
        properties=[prop for prop in entity.properties if prop.name != change.name],
    )


def _without_rel(relation: Relation, change: AttrChange) -> Relation:
    if relation.label != change.owner:
        return relation
    return Relation(
        label=relation.label,
        description=relation.description,
        patterns=list(relation.patterns),
        properties=[prop for prop in relation.properties if prop.name != change.name],
    )
