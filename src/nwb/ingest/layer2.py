"""Layer 2 loader: every business source -> records/documents with classification + per-item grants, then facts.

Steps per item (AGENTS §7.2): canonical hash -> blob (documents) -> one transaction (rows + grants + outbox).
Unchanged items are skipped by hash. Facts then go through the precedence engine in valid-from order.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from nwb.conf.models import SourcesConfig
from nwb.db.models import Document, Grant, Record
from nwb.db.repo_content import ContentRepository
from nwb.ingest.acl import classify_tag, is_personal, needs_grants, parse_acl
from nwb.ingest.frontmatter import split_frontmatter
from nwb.knowledge.clients import ClientCatalogBuilder, ClientRepository
from nwb.knowledge.extract import FactExtractor
from nwb.knowledge.ingest import FactCandidates, LoadedDocument, PendingFact
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.obs.log import asdict_safe as _asdict
from nwb.ports import BlobStore, SqlDatabase
from nwb.temporal.graph_facts import GraphFactBook
from nwb.temporal.precedence import PrecedenceEngine, QuoteMismatchError, check_quote

log = get_logger(__name__)

RECORD_FOLDERS = (
    "approvals",
    "collab",
    "contracts",
    "decisions",
    "engineering",
    "staffing",
    "security_grc",
    "policies",
    "hris",
)
SKIP_FILES = frozenset({"job_events.json", "job_events.csv", "workers.csv"})  # Layer 1 owns these
OWNER_FIELDS = (
    "owner_id",
    "owner",
    "requester_id",
    "employee_id",
    "employeeNumber",
    "subject_worker_id",
    "resource_id",
    "author_id",
    "worker_id",
    "assignee",
    "raised_by",
    "collected_by",
    "case_owner",
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def canonical(item: Any) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def _date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@dataclass
class Layer2Report:
    records_written: int = 0
    records_skipped: int = 0
    documents_written: int = 0
    documents_skipped: int = 0
    grants: int = 0
    facts: dict[str, int] = field(default_factory=dict)
    rejected: int = 0
    clients: int = 0
    extracted_docs: int = 0


class Layer2Loader:
    def __init__(
        self,
        db: SqlDatabase,
        data_dir: Path,
        blobs: BlobStore,
        company_id: str,
        sources: SourcesConfig,
        extractor: FactExtractor | None,
        company_name: str,
        root_unit: str,
        facts: GraphFactBook,
        fallback_level: str = "highly_confidential",
    ) -> None:
        self._db = db
        self._root = data_dir
        self._blobs = blobs
        self._cid = company_id
        self._sources = sources
        self._candidates = FactCandidates(sources, extractor, company_id)
        self._company_name = company_name
        self._root_unit = root_unit
        self._facts = facts
        self._fallback = fallback_level
        self._groups: set[str] = set()
        self._people: set[str] = set()

    # ---- discovery -------------------------------------------------------------
    def _structured_files(self) -> Iterator[tuple[str, Path]]:
        for folder in RECORD_FOLDERS:
            for p in sorted((self._root / folder).glob("*")):
                if p.name in SKIP_FILES or p.suffix not in {".json", ".jsonl", ".csv"}:
                    continue
                yield folder, p

    @staticmethod
    def _items(p: Path) -> list[dict[str, Any]]:
        if p.suffix == ".jsonl":
            return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        if p.suffix == ".csv":
            with p.open(newline="") as fh:
                return [dict(r) for r in csv.DictReader(fh)]
        data = json.loads(p.read_text())
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        return [d for d in data if isinstance(d, dict)]

    # ---- ownership -------------------------------------------------------------
    def _owner(self, item: dict[str, Any], units: dict[str, str]) -> tuple[str | None, str | None]:
        for f in OWNER_FIELDS:
            v = item.get(f)
            pid = _known_person(v, units) if v else None
            if pid is not None:
                return pid, units[pid]
        return None, None

    def _grants(self, rtype: str, rid: str, tag: str | None, acl: Any, created: date | None) -> list[Grant]:
        if not needs_grants(tag):
            return []
        start = created or date.min
        return [
            Grant(
                subject_type=g.subject_type,
                subject_id=g.subject_id,
                resource_type=rtype,  # type: ignore[arg-type]
                resource_id=rid,
                valid_from=g.valid_from or start,
                valid_to=g.valid_to,
            )
            for g in parse_acl(acl, self._groups, self._people)
        ]

    # ---- load --------------------------------------------------------------------
    @traced(step="layer2.load", level=logging.INFO)
    def load(self) -> Layer2Report:
        rep = Layer2Report()
        with self._db.unit_of_work() as uow:
            self._groups = {str(r["id"]) for r in uow.fetchall("SELECT id FROM groups")}
            self._people = {str(r["id"]) for r in uow.fetchall("SELECT id FROM people")}
            units = {
                str(r["person_id"]): str(r["unit_id"])
                for r in uow.fetchall(
                    "SELECT person_id, unit_id FROM memberships WHERE is_primary AND valid_to IS NULL"
                )
            }
        pending: list[PendingFact] = []
        records: list[tuple[Record, list[Grant]]] = []
        for folder, path in self._structured_files():
            rtype = path.stem
            for item in self._items(path):
                rec, grants, facts = self._record(folder, rtype, item, units)
                records.append((rec, grants))
                pending += facts
        documents: list[tuple[Document, list[Grant]]] = []
        loaded: list[LoadedDocument] = []
        clients = ClientCatalogBuilder(self._company_name)
        dsrc = self._sources.documents
        for folder in dsrc.folders:
            for path in sorted((self._root / folder).glob("*.md")):
                doc, grants, meta, text = self._document(folder, path, units)
                documents.append((doc, grants))
                loaded.append(LoadedDocument(folder, doc.id, meta, text, split_frontmatter(text)[1]))
                client = dsrc.field(meta, "client")
                if client:
                    parties = dsrc.field(meta, "parties")
                    clients.add(str(client), [str(p) for p in parties] if isinstance(parties, list) else [], doc.id)
        catalog = clients.build()
        pending += self._candidates.from_documents(loaded, client_ids={c.id for c in catalog})
        rep.extracted_docs = len(self._candidates.extraction_log)

        with self._db.unit_of_work() as uow:
            repo = ContentRepository(uow)
            dataflow(Hop.INGEST, loader="layer2", records=len(records), documents=len(documents), facts=len(pending))
            rep.records_written, rep.records_skipped = repo.upsert_records(records)
            rep.documents_written, rep.documents_skipped = repo.upsert_documents(documents)
            rep.grants = sum(len(g) for _, g in records) + sum(len(g) for _, g in documents)
            rep.clients = ClientRepository(uow).replace_all(self._cid, catalog)

        rep.facts, rep.rejected = self._apply_facts(pending)
        log.info("layer2 loaded: %s", rep)
        dataflow(Hop.SQL_WRITE, loader="layer2", **_asdict(rep))
        return rep

    def _record(
        self, folder: str, rtype: str, item: dict[str, Any], units: dict[str, str]
    ) -> tuple[Record, list[Grant], list[PendingFact]]:
        meta = item.get("x_northwind") if "classification" not in item else item
        meta = meta if isinstance(meta, dict) else {}
        tag = meta.get("classification")
        raw_id = str(item.get("id") or item.get("uuid") or item.get("row_id") or sha(canonical(item))[:16])
        rid = f"{folder}/{rtype}:{raw_id}"
        cls, share = classify_tag(tag)
        owner, unit = self._owner(item, units)
        personal = is_personal(tag) and owner is not None
        if cls == "confidential" and unit is None:
            unit = self._root_unit
        text = canonical(item)
        created = _date(meta.get("created_at") or item.get("published"))
        rec = Record(
            id=rid,
            company_id=self._cid,
            source=f"{folder}/{rtype}",
            source_id=raw_id,
            record_type=rtype,
            owner_person_id=owner,
            owner_unit_id=unit,
            dept_id=unit,
            classification=cls,
            share_scope=share,
            source_tag=tag,
            personal=personal,
            payload=text,
            hash=sha(text),
            occurred_at=str(meta.get("created_at") or item.get("published") or "") or None,
        )
        grants = [] if personal else self._grants("record", rid, tag, meta.get("acl"), created)
        facts = self._candidates.from_record(rtype, item, rid, text)
        return rec, grants, facts

    def _document(
        self, folder: str, path: Path, units: dict[str, str]
    ) -> tuple[Document, list[Grant], dict[str, Any], str]:
        raw = path.read_bytes()
        text = raw.decode()
        meta, _body = split_frontmatter(text)
        tag = meta.get("classification")
        cls, share = classify_tag(tag)
        did = str(meta.get("id") or meta.get("doc_id") or path.stem)
        owner = _known_person(meta.get("owner") or meta.get("authors") or "", units)
        unit = units.get(owner) if owner else None
        if cls == "confidential" and unit is None:
            unit = self._root_unit
        ver_m = re.search(r"(\d+)", str(meta.get("version") or "1"))
        doc = Document(
            id=did,
            company_id=self._cid,
            title=str(meta.get("title") or path.stem),
            doc_type=str(meta.get("type") or folder.split("/")[-1]),
            version=int(ver_m.group(1)) if ver_m else 1,
            owner_person_id=owner,
            owner_unit_id=unit,
            dept_id=unit,
            classification=cls,
            share_scope=share,
            source_tag=tag,
            blob_url=self._blobs.put(raw),
            bytes=len(raw),
            hash=sha(raw),
            source=folder,
            source_id=did,
        )
        grants = self._grants("document", did, tag, meta.get("acl"), _date(meta.get("created_at")))
        return doc, grants, meta, text

    def _apply_facts(self, pending: list[PendingFact]) -> tuple[dict[str, int], int]:
        """Quote-check, then write each fact into the graph of its source's classification."""
        counts: dict[str, int] = {}
        rejected = 0
        ordered = sorted(pending, key=lambda p: (p.fact.valid_from, int(p.fact.authority), p.fact.src_id))
        with self._db.unit_of_work() as uow:
            classes = {
                str(r["id"]): str(r["classification"]) for r in uow.fetchall("SELECT id, classification FROM documents")
            }
            classes.update(
                {str(r["id"]): str(r["classification"]) for r in uow.fetchall("SELECT id, classification FROM records")}
            )
            titles = {str(r["id"]): str(r["title"]) for r in uow.fetchall("SELECT id, title FROM documents")}
        engine = PrecedenceEngine(self._facts)
        for p in ordered:
            try:
                check_quote(p.fact.quote, p.source_text)
            except QuoteMismatchError as exc:
                with self._db.unit_of_work() as uow:
                    ContentRepository(uow).log("facts", p.fact.src_id, "rejected", f"quote_mismatch: {exc}")
                rejected += 1
                continue
            level = classes.get(p.fact.src_id) or self._fallback
            title = titles.get(p.fact.src_id)
            fact = p.fact.with_(title=title) if title else p.fact
            stored = engine.on_new_fact(fact, level)
            counts[stored.status] = counts.get(stored.status, 0) + 1
        return counts, rejected


def _known_person(value: object, people: dict[str, str]) -> str | None:
    """First token in a free-text owner field that is a known person id (no id format assumed)."""
    for tok in _TOKEN.findall(str(value)):
        if str(tok) in people:
            return str(tok)
    return None
