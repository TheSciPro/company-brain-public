"""Turn loaded sources into candidate facts. Generic: the source catalog says how, the data says what.

- Records: ``sources.yaml`` lists which fields of a record type are facts and which fields scope them.
- Documents: the LLM extractor (cached by document hash) proposes facts; authority, draft status, validity and the
  client scope come from the catalog + frontmatter, never from the model. Documents are processed in valid-from
  order so the key catalog grows and later versions reuse earlier keys.

Every candidate still has to pass the quote guard and the precedence engine (``Layer2Loader._apply_facts``).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from nwb.conf.models import RecordSource, SourcesConfig
from nwb.knowledge.extract import DocumentInput, FactExtractor, KnownKey
from nwb.knowledge.scopes import format_scope
from nwb.obs.log import get_logger
from nwb.temporal.facts import Authority, Fact, normalise_value
from nwb.util.geo import normalize_country

log = get_logger(__name__)

AUTHORITY = {
    "chat_or_draft": Authority.CHAT_OR_DRAFT,
    "minutes_or_decision": Authority.MINUTES_OR_DECISION,
    "approved_policy": Authority.APPROVED_POLICY,
    "approved_exception": Authority.APPROVED_EXCEPTION,
    "contract": Authority.CONTRACT,
}


def as_date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class PendingFact:
    fact: Fact
    source_text: str


@dataclass(frozen=True, slots=True)
class LoadedDocument:
    folder: str
    doc_id: str
    meta: dict[str, Any]
    raw: str
    body: str


class FactCandidates:
    def __init__(self, sources: SourcesConfig, extractor: FactExtractor | None, company_id: str) -> None:
        self._src = sources
        self._extractor = extractor
        self._cid = company_id
        self.extraction_log: list[tuple[str, str, int]] = []  # (doc_id, prompt_ref, facts)

    # ---------------------------------------------------------------- records
    def from_record(self, rtype: str, item: dict[str, Any], rid: str, text: str) -> list[PendingFact]:
        spec = self._src.records.get(rtype)
        if spec is None or not spec.facts:
            return []
        start = next((d for f in spec.valid_from if (d := as_date(item.get(f)))), None) or date.min
        out: list[PendingFact] = []
        for rf in spec.facts:
            raw = item.get(rf.field)
            if raw is None or raw == "":
                continue
            scope_vals = {dim: _scope_value(dim, item.get(fld)) for dim, fld in rf.scope.items()}
            out.append(
                PendingFact(
                    Fact(
                        key=rf.field,
                        about=self._cid,
                        value=normalise_value(raw),
                        unit=rf.unit,
                        scope=format_scope(scope_vals, self._src.scope_dimensions),
                        valid_from=start,
                        authority=AUTHORITY[spec.authority],
                        src_type="record",
                        src_id=rid,
                        quote=f'"{rf.field}": {_json_scalar(raw)}',
                        company_id=self._cid,
                        label=rf.field.replace("_", " "),
                    ),
                    text,
                )
            )
        return out

    # ---------------------------------------------------------------- documents
    def from_documents(
        self,
        docs: Iterable[LoadedDocument],
        seed_keys: Iterable[KnownKey] = (),
        client_ids: Collection[str] | None = None,
    ) -> list[PendingFact]:
        """Extracted facts for documents. ``client_ids``: accounts that are real clients (others get no client
        scope: an internal account label on a decision does not make it client-specific)."""
        if self._extractor is None or not self._src.documents.extract:
            return []
        dsrc = self._src.documents
        known: dict[str, KnownKey] = {k.key: k for k in seed_keys}
        # Vocabulary order: folders in catalog order (the catalog lists the most general sources first), then by
        # date. Earlier documents define the fact keys later ones are asked to reuse.
        rank = {f: i for i, f in enumerate(dsrc.folders)}
        ordered = sorted(docs, key=lambda d: (rank.get(d.folder, len(rank)), self._valid_from(d) or date.min, d.doc_id))
        out: list[PendingFact] = []
        for d in ordered:
            status = str(dsrc.field(d.meta, "status") or "")
            client = dsrc.field(d.meta, "client")
            if client is not None and client_ids is not None and str(client) not in client_ids:
                client = None
            ex = self._extractor.extract(
                DocumentInput(
                    doc_id=d.doc_id,
                    title=str(dsrc.field(d.meta, "title") or d.doc_id),
                    doc_type=str(d.meta.get("type") or d.folder),
                    status=status or None,
                    client=str(client) if client else None,
                    raw=d.raw,
                    body=d.body,
                ),
                list(known.values()),
            )
            if ex is None:
                continue
            self.extraction_log.append((d.doc_id, ex.prompt_ref, len(ex.facts)))
            rule = dsrc.authority_for(d.folder, status)
            start = self._valid_from(d) or date.min
            for f in ex.facts:
                known.setdefault(f.key, KnownKey(f.key, f.label, f.unit))
                scope_vals: dict[str, str | None] = {**f.qualifiers, "client": str(client) if client else None}
                out.append(
                    PendingFact(
                        Fact(
                            key=f.key,
                            about=self._cid,
                            value=normalise_value(f.value),
                            unit=f.unit,
                            scope=format_scope(scope_vals, self._src.scope_dimensions),
                            valid_from=start,
                            authority=AUTHORITY[rule.authority],
                            draft=rule.draft,
                            src_type="document",
                            src_id=d.doc_id,
                            quote=f.quote,
                            company_id=self._cid,
                            label=f.label,
                        ),
                        d.raw,
                    )
                )
        return out

    def _valid_from(self, d: LoadedDocument) -> date | None:
        return as_date(self._src.documents.field(d.meta, "valid_from"))


def _scope_value(dim: str, v: Any) -> str | None:
    if v in (None, ""):
        return None
    return normalize_country(str(v)) if dim == "country" else str(v)


def _json_scalar(v: Any) -> str:
    return str(v) if isinstance(v, int | float) else f'"{v}"'


def assignment_rows(spec: RecordSource, items: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Project assignment records onto (person, client, start, end, active) using the catalog's field mapping."""
    for it in items:
        status = str(it.get(spec.f("status")) or "") if "status" in spec.fields else ""
        yield {
            "person": it.get(spec.f("person")),
            "client": it.get(spec.f("client")),
            "start": as_date(it.get(spec.f("start"))),
            "end": as_date(it.get(spec.f("end"))),
            "active": not any(status.startswith(s) for s in spec.inactive_status),
        }
