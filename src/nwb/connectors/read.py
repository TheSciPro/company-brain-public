"""Read-only view of one source. This object has no write method and issues only SELECT statements.

Connected-app payloads are mapped from the field names in ``config/sources.yaml``. The network client is
injected; nothing here reads an API key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from nwb.conf.models import ConnectorDecl, SearchConfig
from nwb.connectors.catalog import SourceSpec
from nwb.connectors.ports import ComposioClient, Principal, SourceConnector, SourceItem
from nwb.identity import Actor
from nwb.obs.log import Hop, dataflow, get_logger
from nwb.ports import AccessPolicy, BlobStore, SqlDatabase

log = get_logger(__name__)

_DOCS_SQL = (
    "SELECT id, title, blob_url FROM documents WHERE company_id = :company AND source = :source AND deleted_at IS NULL"
)
_RECORDS_SQL = "SELECT id, record_type, payload FROM records WHERE company_id = :company AND record_type = :record_type"
_CURRENT = "m.valid_from <= :asof AND (m.valid_to IS NULL OR m.valid_to >= :asof)"
# The directory is internal: same rule as ``nw_can_read`` for an internal row. The asker needs a current
# membership on the ask date, and only people with a membership on that date are listed.
_ASKER_SQL = (
    "SELECT me.id FROM people me JOIN memberships m ON m.person_id = me.id "
    f"WHERE me.id = :me AND me.company_id = :company AND me.status = 'active' AND {_CURRENT}"
)
_PEOPLE_SQL = (
    "SELECT p.id, p.name, p.title FROM people p WHERE p.company_id = :company AND p.status = 'active' "
    f"AND EXISTS (SELECT 1 FROM memberships m WHERE m.person_id = p.id AND {_CURRENT})"
)
_EMAIL_SQL = "SELECT email FROM people WHERE company_id = :company AND id = :id"
_ACCOUNTS_SQL = "SELECT subject FROM user_identities WHERE person_id = :id AND provider = :provider"

_SELECTS = (_DOCS_SQL, _RECORDS_SQL, _ASKER_SQL, _PEOPLE_SQL, _EMAIL_SQL, _ACCOUNTS_SQL)


class SourceHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    title: str
    snippet: str
    source: str


class SourceRead(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    hits: list[SourceHit]
    note: str | None = None


class ReadOnlySource:
    """Search one source. Callers that need to change it use a separate ``ActionSink``."""

    def __init__(
        self,
        spec: SourceSpec,
        db: SqlDatabase,
        policy: AccessPolicy,
        blobs: BlobStore,
        terms: SearchConfig,
        client_of: Callable[[], ComposioClient | None],
    ) -> None:
        self.spec = spec
        self._db = db
        self._policy = policy
        self._blobs = blobs
        self._terms = terms
        self._client_of = client_of

    def search(self, actor: Actor, question: str, limit: int) -> SourceRead:
        terms = self._terms.terms(question)
        if not terms:
            return self._empty("query has no searchable words")
        if self.spec.kind == "composio" and (self._client_of() is None or self.spec.connector is None):
            return self._empty("This source is not connected")
        if self.spec.kind == "composio":
            ranked = self._composio(actor, terms)
        elif self.spec.kind == "documents":
            ranked = self._documents(actor, terms)
        elif self.spec.kind == "records":
            ranked = self._records(actor, terms)
        else:
            ranked = self._directory(actor, terms)
        # Rank on the full text. The snippet is a window and would bury a real match.
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        kept = [hit for _, hit in ranked[:limit]]
        note = None if kept else "Nothing you can see in this source matches."
        dataflow(Hop.RETRIEVAL, source=self.spec.id, hits=len(kept), kind="source")
        return SourceRead(source=self.spec.id, hits=kept, note=_scoped(note))

    def _empty(self, note: str) -> SourceRead:
        dataflow(Hop.RETRIEVAL, source=self.spec.id, hits=0, kind="source")
        return SourceRead(source=self.spec.id, hits=[], note=_scoped(note))

    def _documents(self, actor: Actor, terms: list[str]) -> list[tuple[float, SourceHit]]:
        visible = set(self._policy.visible_document_ids(actor.person_id, actor.asof))
        with self._db.unit_of_work() as uow:
            rows = uow.fetchall(_DOCS_SQL, {"company": actor.company_id, "source": self.spec.folder})
        hits: list[tuple[float, SourceHit]] = []
        for row in rows:
            if str(row["id"]) not in visible:
                continue
            body = _blob(self._blobs, str(row["blob_url"]))
            text = f"{row['title']} {body}"
            score = _score(text, terms)
            if score < 0.5:
                continue
            hits.append(
                (
                    score,
                    SourceHit(
                        id=str(row["id"]),
                        title=str(row["title"]),
                        snippet=_snippet(_prose(body) or str(row["title"]), terms),
                        source=self.spec.id,
                    ),
                )
            )
        return hits

    def _records(self, actor: Actor, terms: list[str]) -> list[tuple[float, SourceHit]]:
        visible = set(self._policy.visible_record_ids(actor.person_id, actor.asof))
        with self._db.unit_of_work() as uow:
            rows = uow.fetchall(_RECORDS_SQL, {"company": actor.company_id, "record_type": self.spec.record_type})
        hits: list[tuple[float, SourceHit]] = []
        for row in rows:
            if str(row["id"]) not in visible:
                continue
            text = str(row["payload"])
            score = _score(text, terms)
            if score < 0.5:
                continue
            hits.append(
                (
                    score,
                    SourceHit(
                        id=str(row["id"]),
                        title=str(row["record_type"]),
                        snippet=_snippet(text, terms),
                        source=self.spec.id,
                    ),
                )
            )
        return hits

    def _directory(self, actor: Actor, terms: list[str]) -> list[tuple[float, SourceHit]]:
        scope = {"company": actor.company_id, "asof": actor.asof}
        with self._db.unit_of_work() as uow:
            if uow.fetchone(_ASKER_SQL, {**scope, "me": actor.person_id}) is None:
                return []
            rows = uow.fetchall(_PEOPLE_SQL, scope)
        hits: list[tuple[float, SourceHit]] = []
        for row in rows:
            text = f"{row['name']} {row['title'] or ''}"
            score = _score(text, terms)
            if score < 0.5:
                continue
            hits.append(
                (
                    score,
                    SourceHit(
                        id=str(row["id"]),
                        title=str(row["name"]),
                        snippet=str(row["title"] or ""),
                        source=self.spec.id,
                    ),
                )
            )
        return hits

    def _composio(self, actor: Actor, terms: list[str]) -> list[tuple[float, SourceHit]]:
        client = self._client_of()
        connector = self.spec.connector
        if client is None or connector is None:
            return []
        refs = _reader_refs(self._db, actor, connector.identity_provider)
        hits: list[tuple[float, SourceHit]] = []
        for item in _ComposioItems(connector, client).items(None):
            if not _reader_match(item, refs):
                continue
            text = f"{item.title} {item.body}"
            score = _score(text, terms)
            if score < 0.5:
                continue
            hits.append(
                (
                    score,
                    SourceHit(
                        id=item.external_id,
                        title=item.title,
                        snippet=_snippet(item.body or item.title, terms),
                        source=self.spec.id,
                    ),
                )
            )
        return hits


class _ComposioItems(SourceConnector):
    """``SourceConnector`` over an injected client. Listing is the read; there is no write here."""

    def __init__(self, spec: ConnectorDecl, client: ComposioClient) -> None:
        self.name = spec.id
        self._spec = spec
        self._client = client

    def items(self, since: datetime | None) -> Iterator[SourceItem]:
        yield from iter_items(self._spec, self._client, since)


def iter_items(spec: ConnectorDecl, client: ComposioClient, since: datetime | None = None) -> Iterator[SourceItem]:
    params: dict[str, Any] = {spec.limit_param: spec.max_results}
    if since is not None and spec.since_query:
        params["query"] = spec.since_query.replace("{unix}", str(int(since.timestamp())))
    if spec.channel_param is None:
        yield from _page(spec, client, params)
        return
    if not spec.channels:
        log.info("connector has no readable conversations configured", source=spec.id)
        return
    for channel in spec.channels:
        yield from _page(spec, client, {**params, spec.channel_param: channel})


def _page(spec: ConnectorDecl, client: ComposioClient, params: Mapping[str, Any]) -> Iterator[SourceItem]:
    data = client.execute(spec.list_action, params)
    raw = data.get(spec.items_key, [])
    if not isinstance(raw, list):
        log.warning("connector %s returned no item list under %s", spec.id, spec.items_key)
        return
    for msg in raw:
        if isinstance(msg, Mapping):
            item = map_payload(spec, msg)
            if item is not None:
                yield item


def map_payload(spec: ConnectorDecl, msg: Mapping[str, Any]) -> SourceItem | None:
    """Map one provider payload using the catalog field names. Missing ids are dropped."""
    fields = spec.fields
    external_id = str(msg.get(fields.external_id) or "").strip()
    if not external_id:
        return None
    title = str(msg.get(fields.title) or "").strip() or "(no subject)"
    body = str(msg.get(fields.body) or "")
    author = str(msg.get(fields.author) or "").strip() if fields.author else ""
    readers: dict[str, Principal] = {}
    for key in fields.readers:
        raw = msg.get(key)
        if raw is None:
            continue
        for part in str(raw).split(","):
            ref = part.strip()
            if ref:
                readers.setdefault(ref, Principal(kind="person", ref=ref))
    return SourceItem(
        source=spec.id,
        external_id=external_id,
        title=title,
        body=body,
        author=author or None,
        created_at=_when(msg.get(fields.created_at) if fields.created_at else None),
        readers=tuple(sorted(readers.values(), key=lambda principal: principal.ref)),
    )


def _when(value: object) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _email(db: SqlDatabase, actor: Actor) -> str | None:
    with db.unit_of_work() as uow:
        row = uow.fetchone(_EMAIL_SQL, {"company": actor.company_id, "id": actor.person_id})
    if row is None or not row.get("email"):
        return None
    return str(row["email"])


def _reader_refs(db: SqlDatabase, actor: Actor, provider: str | None) -> set[str]:
    """Who this person is in the app's reader fields.

    With an ``identity_provider`` the app's user ids are mapped through ``user_identities`` and nothing
    else counts, so an app id is never compared with our person id. Without one, our id and email.
    """
    if provider is not None:
        with db.unit_of_work() as uow:
            rows = uow.fetchall(_ACCOUNTS_SQL, {"id": actor.person_id, "provider": provider})
        return {str(row["subject"]) for row in rows if row.get("subject")}
    email = _email(db, actor)
    return {actor.person_id, email} if email else {actor.person_id}


def _reader_match(item: SourceItem, refs: set[str]) -> bool:
    """Personal items are visible only to a listed reader. No readers means hidden (fail closed)."""
    allowed = {ref.lower() for ref in refs}
    return any(principal.ref.lower() in allowed for principal in item.readers)


def _blob(blobs: BlobStore, url: str) -> str:
    try:
        return blobs.get(url).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        log.warning("blob unreadable for source read")
        return ""


def _score(text: str, terms: list[str]) -> float:
    low = text.lower()
    return sum(1 for term in terms if term in low) / len(terms)


def _prose(text: str) -> str:
    """Drop a leading markdown frontmatter block so the snippet is the document, not its header."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3 and parts[2].strip():
            return parts[2].strip()
    return text


def _snippet(text: str, terms: list[str], width: int = 360) -> str:
    low = text.lower()
    pos = min((found for term in terms if (found := low.find(term)) >= 0), default=0)
    start = max(0, pos - width // 3)
    return " ".join(text[start : start + width].split())


def _scoped(note: str | None) -> str:
    tail = "Only this source was searched."
    return tail if not note else f"{note} {tail}"
