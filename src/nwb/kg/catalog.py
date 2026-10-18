"""Split configured sources into one working directory per access level.

Tabular files become CSV (the SDK's structured ingest). Prose keeps its relative path so a
recording key does not contain a machine temp directory. This module does not open a graph.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nwb.conf.models import GraphLoadConfig, TableDecl
from nwb.errors import DataLoadError
from nwb.ingest.acl import classify_tag
from nwb.ingest.frontmatter import split_frontmatter
from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class TableSlice:
    source_name: str
    path: Path
    rows: int
    decl: TableDecl


@dataclass(frozen=True)
class ProseDoc:
    document_id: str
    path: Path
    text: str


@dataclass
class LevelBundle:
    level: str
    directory: Path
    tables: list[TableSlice] = field(default_factory=list)
    prose: list[ProseDoc] = field(default_factory=list)

    @property
    def table_rows(self) -> int:
        return sum(table.rows for table in self.tables)


def materialise(
    data_dir: Path,
    load: GraphLoadConfig,
    work_dir: Path,
    levels: tuple[str, ...],
    *,
    max_rows: int | None = None,
    max_prose: int = 0,
    tables_only: bool = False,
) -> list[LevelBundle]:
    """Write one directory per access level that actually has rows or prose.

    ``max_rows`` caps each table (tests and a cheap smoke load). ``max_prose`` caps
    documents per level; ``0`` means every matching file. ``tables_only`` skips prose.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    by_level: dict[str, LevelBundle] = {level: LevelBundle(level, work_dir / level) for level in levels}
    for decl in load.tables:
        _write_table(data_dir, decl, load, by_level, max_rows=max_rows)
    if not tables_only:
        _write_prose(data_dir, load, by_level, max_prose=max_prose)
    bundles = [bundle for bundle in by_level.values() if bundle.tables or bundle.prose]
    dataflow(
        Hop.INGEST,
        stage="catalog",
        levels=len(bundles),
        table_rows=sum(bundle.table_rows for bundle in bundles),
        prose_docs=sum(len(bundle.prose) for bundle in bundles),
    )
    return bundles


def _write_table(
    data_dir: Path,
    decl: TableDecl,
    load: GraphLoadConfig,
    by_level: dict[str, LevelBundle],
    *,
    max_rows: int | None,
) -> None:
    source = data_dir / decl.path
    if not source.is_file():
        raise DataLoadError(f"configured table not found: {decl.path}")
    rows = _read_rows(source, decl.format)
    if max_rows is not None:
        rows = rows[:max_rows]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    columns = _columns(decl)
    for raw in rows:
        key = _cell(raw.get(decl.key))
        if not key:
            continue
        tag = _tag(raw, load.classification_field, load.envelope_keys)
        level, _share = classify_tag(tag)
        if level not in by_level:
            raise DataLoadError(f"tag {tag!r} resolved to {level!r}, which is not a configured level")
        grouped[level].append({col: _cell(raw.get(col)) for col in columns})
    for level, records in grouped.items():
        bundle = by_level[level]
        bundle.directory.mkdir(parents=True, exist_ok=True)
        path = bundle.directory / decl.source_name
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(records)
        bundle.tables.append(TableSlice(decl.source_name, path, len(records), decl))
        log.info("table sliced", source=decl.path, level=level, rows=len(records))


def _write_prose(data_dir: Path, load: GraphLoadConfig, by_level: dict[str, LevelBundle], *, max_prose: int) -> None:
    found: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for decl in load.prose:
        root = data_dir / decl.root
        if not root.is_dir():
            log.info("prose root missing; skipping", root=decl.root)
            continue
        for path in sorted(root.rglob(decl.pattern)):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            meta, body = split_frontmatter(text)
            tag = meta.get(decl.classification_field)
            tag_s = str(tag) if tag not in (None, "") else None
            level, _share = classify_tag(tag_s)
            if level not in by_level:
                raise DataLoadError(f"tag {tag_s!r} resolved to {level!r}, which is not a configured level")
            rel = path.relative_to(data_dir).as_posix()
            found[level].append((rel, body.strip() or text.strip()))
    for level, docs in found.items():
        chosen = docs if max_prose <= 0 else docs[:max_prose]
        bundle = by_level[level]
        bundle.directory.mkdir(parents=True, exist_ok=True)
        for rel, body in chosen:
            dest = bundle.directory / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body + "\n", encoding="utf-8")
            bundle.prose.append(ProseDoc(rel, dest, body))


def _columns(decl: TableDecl) -> list[str]:
    ordered = [decl.key]
    if decl.name != decl.key:
        ordered.append(decl.name)
    ordered.extend(decl.properties)
    for link in decl.links:
        ordered.append(link.by)
        if link.name and link.name not in ordered:
            ordered.append(link.name)
    seen: set[str] = set()
    out: list[str] = []
    for col in ordered:
        if col not in seen:
            seen.add(col)
            out.append(col)
    return out


def _read_rows(path: Path, fmt: str) -> list[dict[str, Any]]:
    if fmt == "csv":
        with path.open(newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    if fmt == "jsonl":
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
        return rows
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise DataLoadError(f"{path.name} must be a JSON list of objects")
    return list(data)


def _tag(row: dict[str, Any], field: str, envelopes: tuple[str, ...]) -> str | None:
    direct = row.get(field)
    if direct not in (None, ""):
        return str(direct)
    for key in envelopes:
        env = row.get(key)
        if isinstance(env, dict) and env.get(field) not in (None, ""):
            return str(env[field])
    return None


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value).strip()
