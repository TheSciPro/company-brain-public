"""Company facts in the per-level FalkorDB graphs.

Each fact is a node in ``{company}_kb_{level}`` (the same graphs GraphRAG-SDK loads). The node carries both
clocks: ``valid_at`` / ``invalid_at`` (when it was true) and ``recorded_at`` (when we learned it, epoch ms).
A newer fact points at the old one with an edge. The old node stays, with ``invalid_at`` set. Nothing is deleted.

Reads are parameterised Cypher through FalkorDB ``ro_query``. Which sources a person may see comes from
Postgres row-level security on ``documents`` and ``records`` (migration 0004, or the SQLite equivalent).

Access for an answer is the set of source ids the caller can read. The query never counts the ones it skips.
"""

from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Any

from redislite.falkordb_client import FalkorDB as FalkorLite

from nwb.conf.models import GraphConfig
from nwb.errors import ConfigError
from nwb.graph.client import FalkorGraphStore
from nwb.temporal.facts import Authority, Fact

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ConfigError(f"fact graph name must be an identifier, got {name!r}")
    return name


class GraphFactBook:
    """Read and write fact nodes across the per-level graphs on one FalkorDB."""

    def __init__(
        self,
        db: Any,
        cfg: GraphConfig,
        levels: tuple[str, ...],
        company: str,
        prefix: str,
        timeout_ms: int | None = None,
    ) -> None:
        self._db = db
        self._cfg = cfg
        self._clock = cfg.company.facts
        self._levels = levels
        self._extra: list[str] = []
        self._company = company
        self._prefix = prefix
        self._timeout = timeout_ms if timeout_ms is not None else cfg.company.facts.query_timeout_ms
        self._stores: dict[str, FalkorGraphStore] = {}
        self._ready: set[str] = set()

    @classmethod
    def open_lite(
        cls,
        path: Any,
        *,
        company: str = "NW",
        levels: tuple[str, ...] = ("public", "internal", "confidential", "highly_confidential"),
        timeout_ms: int = 10_000,
    ) -> GraphFactBook:
        """Embedded FalkorDBLite for tests and local ingest. The caller keeps this object alive."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return cls(FalkorLite(str(dest)), GraphConfig(version=1), levels, company, "", timeout_ms)

    def release(self) -> None:
        """Flush the embedded store. A server client is left to the composition root."""
        closer = getattr(self._db, "close", None)
        if closer is not None:
            closer()

    # ---------------------------------------------------------------- graphs
    @property
    def live_statuses(self) -> tuple[str, ...]:
        return self._clock.live_statuses

    def _names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self._levels, *self._extra)))

    def _graph_name(self, level: str) -> str:
        return self._cfg.company_graph(self._company, level, prefix=self._prefix)

    def _store(self, level: str) -> FalkorGraphStore:
        if level not in self._levels and level not in self._extra:
            self._extra.append(level)
        name = self._graph_name(level)
        store = self._stores.get(name)
        if store is None:
            store = FalkorGraphStore(self._db.select_graph(name), self._timeout)
            self._stores[name] = store
        if name not in self._ready:
            self._ensure(store)
            self._ready.add(name)
        return store

    def _ensure(self, store: FalkorGraphStore) -> None:
        label = _ident(self._clock.label)
        for prop in ("id", "key"):
            try:
                store.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception as exc:
                if "already indexed" not in str(exc).lower():
                    raise

    # ---------------------------------------------------------------- writes
    def get(self, fact_id: str) -> Fact | None:
        for level in self._names():
            found = self._rows(level, "f.id = $id", {"id": fact_id}, limit=1, order=False)
            if found:
                return found[0]
        return None

    def save(self, fact: Fact, level: str) -> None:
        """Insert or replace the node. Edges are added only when the other fact is in this same graph."""
        stored = fact if fact.level == level else fact.with_(level=level)
        self._upsert(level, stored)
        if stored.supersedes_id:
            self._relate(level, stored.id, stored.supersedes_id, self._clock.supersedes)
        if stored.overrides_id:
            self._relate(level, stored.id, stored.overrides_id, self._clock.overrides, scope=stored.scope)
        if stored.related_id and stored.status in self._clock.contested_statuses:
            self._relate(level, stored.id, stored.related_id, self._clock.contradicts)

    def close(self, fact: Fact, valid_to: date) -> Fact:
        """Set ``invalid_at``. The node stays so an older as-of date can still read it."""
        level = fact.level or self._level_of(fact.id)
        if level is None:
            raise ConfigError(f"fact {fact.id} is not in a graph")
        label = _ident(self._clock.label)
        invalid = _ident(self._clock.invalid_at)
        self._store(level).query(
            f"MATCH (f:{label} {{id: $id}}) SET f.{invalid} = date($end), f.status = 'superseded'",
            {"id": fact.id, "end": valid_to.isoformat()},
        )
        return fact.with_(valid_to=valid_to, status="superseded", level=level)

    def add_alert(self, kind: str, fact: Fact, other: Fact, detail: str) -> None:
        level = fact.level or self._level_of(fact.id) or (self._levels[0] if self._levels else "internal")
        label = _ident(self._clock.label)
        alert = _ident(self._clock.alert_label)
        about = _ident(self._clock.about)
        self._store(level).query(
            f"MERGE (a:{alert} {{id: $id}}) SET a.type = $type, a.detail = $detail, a.recorded_at = $recorded "
            f"WITH a MATCH (f:{label} {{id: $fid}}) MERGE (a)-[:{about}]->(f)",
            {
                "id": f"AL-{kind}-{fact.id}",
                "type": kind,
                "detail": detail,
                "recorded": _now(),
                "fid": fact.id,
            },
        )
        if other.level == level or self._level_of(other.id) == level:
            self._store(level).query(
                f"MATCH (a:{alert} {{id: $id}}) MATCH (o:{label} {{id: $oid}}) MERGE (a)-[:{about}]->(o)",
                {"id": f"AL-{kind}-{fact.id}", "oid": other.id},
            )

    # ---------------------------------------------------------------- reads
    def valid_at(
        self,
        key: str,
        about: str,
        scope: str | None,
        at: date,
        statuses: tuple[str, ...],
        src_ids: set[str] | None = None,
    ) -> list[Fact]:
        """Facts whose validity covers ``at``, best first (authority, then newest)."""
        if src_ids is not None and not src_ids:
            return []
        found: list[Fact] = []
        for level in self._names():
            where, params = self._window(key, about, scope, at, statuses, src_ids)
            found.extend(self._rows(level, where, params, limit=8))
        found.sort(key=lambda f: (-int(f.authority), -f.valid_from.toordinal(), f.id))
        return found

    def contested(self, key: str, about: str, scope: str | None, src_ids: set[str] | None = None) -> list[Fact]:
        if src_ids is not None and not src_ids:
            return []
        where, params = self._status_filter(self._clock.contested_statuses)
        where = f"f.key = $key AND f.about = $about AND {where}"
        params.update({"key": key, "about": about})
        if scope is None:
            where += " AND f.scope IS NULL"
        else:
            where += " AND (f.scope IS NULL OR f.scope = $scope)"
            params["scope"] = scope
        if src_ids is not None:
            where += " AND f.src_id IN $src"
            params["src"] = list(src_ids)
        found: list[Fact] = []
        for level in self._names():
            found.extend(self._rows(level, where, params, limit=20, order=False))
        found.sort(key=lambda f: (f.valid_from, f.id))
        return found

    def history(self, key: str, about: str, src_ids: set[str] | None = None) -> list[Fact]:
        if src_ids is not None and not src_ids:
            return []
        where, params = "f.key = $key AND f.about = $about", {"key": key, "about": about}
        if src_ids is not None:
            where += " AND f.src_id IN $src"
            params["src"] = list(src_ids)
        found: list[Fact] = []
        for level in self._names():
            found.extend(self._rows(level, where, params, limit=50, order=False))
        found.sort(key=lambda f: (f.valid_from, int(f.authority), f.id))
        return found

    def catalog(self, src_ids: set[str]) -> list[dict[str, Any]]:
        """Key, label, unit, quote and title for resolution, from facts whose source is in ``src_ids``.

        The caller passes the sources one person may read, so a hidden fact's key or label never reaches them.
        """
        if not src_ids:
            return []
        where, params = self._status_filter(self._clock.index_statuses)
        where += " AND f.src_id IN $src"
        params["src"] = list(src_ids)
        rows: list[dict[str, Any]] = []
        label = _ident(self._clock.label)
        for level in self._names():
            raw = self._store(level).ro_query(
                f"MATCH (f:{label}) WHERE {where} RETURN f.key, f.label, f.unit, f.quote, f.title",
                params,
            )
            for key, lab, unit, quote, title in raw:
                rows.append({"key": key, "label": lab, "unit": unit, "quote": quote or "", "title": title})
        return rows

    def find_key(self, src_id: str, value: str) -> str | None:
        label = _ident(self._clock.label)
        for level in self._names():
            rows = self._store(level).ro_query(
                f"MATCH (f:{label} {{src_id: $src, value: $value}}) RETURN f.key LIMIT 1",
                {"src": src_id, "value": value},
            )
            if rows:
                return str(rows[0][0])
        return None

    def document_scopes(self) -> set[str]:
        label = _ident(self._clock.label)
        scopes: set[str] = set()
        for level in self._names():
            rows = self._store(level).ro_query(
                f"MATCH (f:{label}) WHERE f.src_type = 'document' AND f.scope IS NOT NULL RETURN DISTINCT f.scope"
            )
            scopes.update(str(row[0]) for row in rows if row and row[0])
        return scopes

    def alert_count(self) -> int:
        alert = _ident(self._clock.alert_label)
        total = 0
        for level in self._names():
            if self._graph_name(level) not in self._ready and level not in self._extra:
                # A level we never wrote has no alerts. Opening it would create an empty graph.
                continue
            rows = self._store(level).ro_query(f"MATCH (a:{alert}) RETURN count(a)")
            total += int(rows[0][0]) if rows else 0
        return total

    # ---------------------------------------------------------------- cypher
    def _window(
        self,
        key: str,
        about: str,
        scope: str | None,
        at: date,
        statuses: tuple[str, ...],
        src_ids: set[str] | None,
    ) -> tuple[str, dict[str, Any]]:
        clock = self._clock
        valid = _ident(clock.valid_at)
        invalid = _ident(clock.invalid_at)
        status, params = self._status_filter(statuses)
        where = (
            f"f.key = $key AND f.about = $about AND {status} "
            f"AND f.{valid} <= date($asof) AND (f.{invalid} IS NULL OR f.{invalid} >= date($asof))"
        )
        params.update({"key": key, "about": about, "asof": at.isoformat()})
        if scope is None:
            where += " AND f.scope IS NULL"
        else:
            where += " AND f.scope = $scope"
            params["scope"] = scope
        if src_ids is not None:
            where += " AND f.src_id IN $src"
            params["src"] = list(src_ids)
        return where, params

    def _status_filter(self, statuses: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
        allowed = set(self._clock.live_statuses) | set(self._clock.contested_statuses) | set(self._clock.index_statuses)
        parts: list[str] = []
        params: dict[str, Any] = {}
        for i, status in enumerate(statuses):
            if status not in allowed:
                raise ConfigError(f"fact status {status!r} is not in graph.yaml facts statuses")
            params[f"st{i}"] = status
            parts.append(f"f.status = $st{i}")
        if not parts:
            raise ConfigError("fact status filter is empty")
        return "(" + " OR ".join(parts) + ")", params

    def _select(self) -> str:
        clock = self._clock
        dated = {_ident(clock.valid_at), _ident(clock.invalid_at)}
        names = [
            "id",
            "key",
            "about",
            "scope",
            "value",
            "unit",
            "status",
            "authority",
            "src_type",
            "src_id",
            "quote",
            "label",
            "title",
            "supersedes_id",
            "overrides_id",
            "related_id",
            "level",
            "company_id",
            clock.valid_at,
            clock.invalid_at,
            clock.recorded_at,
        ]
        bits: list[str] = []
        seen: set[str] = set()
        for name in names:
            ident = _ident(name)
            if ident in seen:
                continue
            seen.add(ident)
            bits.append(f"toString(f.{ident})" if ident in dated else f"f.{ident}")
        return ", ".join(bits)

    def _rows(self, level: str, where: str, params: dict[str, Any], limit: int, order: bool = True) -> list[Fact]:
        label = _ident(self._clock.label)
        valid = _ident(self._clock.valid_at)
        ordering = f" ORDER BY f.authority DESC, f.{valid} DESC, f.id" if order else " ORDER BY f.id"
        cap = max(1, min(int(limit), 50))
        match = f"MATCH (f:{label}) WHERE {where}"
        raw = self._store(level).ro_query(f"{match} RETURN {self._select()}{ordering} LIMIT {cap}", params)
        graph = self._graph_name(level)
        return [
            fact.with_(graph=graph, node_label=self._clock.label, query=match)
            for row in raw
            if (fact := self._fact(level, row)) is not None
        ]

    def _fact(self, level: str, row: list[Any]) -> Fact | None:
        clock = self._clock
        names = self._column_names()
        if len(row) < len(names):
            return None
        data = dict(zip(names, row, strict=False))
        valid = _as_date(data.get(clock.valid_at))
        if valid is None or not data.get("id") or not data.get("key"):
            return None
        status = str(data.get("status") or "current")
        return Fact(
            key=str(data["key"]),
            about=str(data.get("about") or self._company),
            value=str(data.get("value") or ""),
            valid_from=valid,
            authority=Authority(int(data.get("authority") or 1)),
            src_type="record" if data.get("src_type") == "record" else "document",
            src_id=str(data.get("src_id") or ""),
            quote=str(data.get("quote") or ""),
            company_id=str(data.get("company_id") or self._company),
            scope=str(data["scope"]) if data.get("scope") else None,
            unit=str(data["unit"]) if data.get("unit") else None,
            draft=status == "draft",
            valid_to=_as_date(data.get(clock.invalid_at)),
            status=_status(status),
            supersedes_id=_str(data.get("supersedes_id")),
            overrides_id=_str(data.get("overrides_id")),
            related_id=_str(data.get("related_id")),
            label=_str(data.get("label")),
            title=_str(data.get("title")),
            level=str(data.get("level") or level),
            recorded_at=int(data["recorded_at"]) if data.get("recorded_at") is not None else None,
        )

    def _column_names(self) -> list[str]:
        clock = self._clock
        names = [
            "id",
            "key",
            "about",
            "scope",
            "value",
            "unit",
            "status",
            "authority",
            "src_type",
            "src_id",
            "quote",
            "label",
            "title",
            "supersedes_id",
            "overrides_id",
            "related_id",
            "level",
            "company_id",
            clock.valid_at,
            clock.invalid_at,
            clock.recorded_at,
        ]
        out: list[str] = []
        for name in names:
            if name not in out:
                out.append(name)
        return out

    def _upsert(self, level: str, fact: Fact) -> None:
        clock = self._clock
        label = _ident(clock.label)
        valid = _ident(clock.valid_at)
        invalid = _ident(clock.invalid_at)
        recorded = _ident(clock.recorded_at)
        sets = [
            "f.key = $key",
            "f.about = $about",
            "f.scope = $scope",
            "f.value = $value",
            "f.unit = $unit",
            "f.status = $status",
            "f.authority = $authority",
            "f.src_type = $src_type",
            "f.src_id = $src_id",
            "f.quote = $quote",
            "f.label = $label",
            "f.title = $title",
            "f.supersedes_id = $supersedes_id",
            "f.overrides_id = $overrides_id",
            "f.related_id = $related_id",
            "f.level = $level",
            "f.company_id = $company_id",
            f"f.{valid} = date($valid_at)",
            f"f.{recorded} = $recorded_at",
        ]
        params: dict[str, Any] = {
            "id": fact.id,
            "key": fact.key,
            "about": fact.about,
            "scope": fact.scope,
            "value": fact.value,
            "unit": fact.unit,
            "status": fact.status,
            "authority": int(fact.authority),
            "src_type": fact.src_type,
            "src_id": fact.src_id,
            "quote": fact.quote,
            "label": fact.label,
            "title": fact.title,
            "supersedes_id": fact.supersedes_id,
            "overrides_id": fact.overrides_id,
            "related_id": fact.related_id,
            "level": level,
            "company_id": fact.company_id,
            "valid_at": fact.valid_from.isoformat(),
            "recorded_at": fact.recorded_at if fact.recorded_at is not None else _now(),
        }
        if fact.valid_to is None:
            sets.append(f"f.{invalid} = null")
        else:
            sets.append(f"f.{invalid} = date($invalid_at)")
            params["invalid_at"] = fact.valid_to.isoformat()
        self._store(level).query(f"MERGE (f:{label} {{id: $id}}) SET " + ", ".join(sets), params)

    def _relate(self, level: str, src: str, dst: str, edge: str, scope: str | None = None) -> None:
        """Append-only pointer. No edge when the older fact lives in a different graph (no copied node)."""
        label = _ident(self._clock.label)
        kind = _ident(edge)
        extra = "SET e.scope = $scope" if scope is not None else ""
        params: dict[str, Any] = {"id": src, "old": dst}
        if scope is not None:
            params["scope"] = scope
        self._store(level).query(
            f"MATCH (f:{label} {{id: $id}}) MATCH (o:{label} {{id: $old}}) MERGE (f)-[e:{kind}]->(o) {extra}",
            params,
        )

    def _level_of(self, fact_id: str) -> str | None:
        found = self.get(fact_id)
        return found.level if found else None


def _now() -> int:
    return time.time_ns() // 1_000_000


def _as_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _status(value: str) -> Any:
    if value in {"current", "superseded", "conflict", "draft", "corroborates"}:
        return value
    return "current"
