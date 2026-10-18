"""One configured source -> one spec. No per-source class: folders, record types, the directory and
connected apps are all entries in ``config/sources.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nwb.conf.models import ConnectorDecl, SourcesConfig
from nwb.errors import ConfigError

SourceKind = Literal["documents", "records", "directory", "composio"]
_SLUG = re.compile(r"[^a-z0-9]+")


def agent_name(source_id: str) -> str:
    """Subagent name: hyphens, matching the deepagents name pattern."""
    name = _SLUG.sub("-", source_id.strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ConfigError(f"source id {source_id!r} is not a usable subagent name")
    return name


def tool_name(prefix: str, source_id: str) -> str:
    return f"{prefix}_{agent_name(source_id).replace('-', '_')}"


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One source the main agent can delegate to."""

    id: str
    agent_name: str
    query_tool: str
    kind: SourceKind
    label: str
    writes: tuple[str, ...] = ()
    folder: str | None = None
    record_type: str | None = None
    connector: ConnectorDecl | None = None

    @property
    def update_tool(self) -> str | None:
        return tool_name("update", self.id) if self.writes else None

    @property
    def update_kind(self) -> str:
        return self.connector.update_kind if self.connector else "update"


def list_sources(cfg: SourcesConfig) -> list[SourceSpec]:
    """Every source in the catalog, in a stable order. Adding a source is a config change only."""
    specs = [_plain(folder, "documents", folder=folder) for folder in cfg.documents.folders]
    specs.extend(_plain(record_type, "records", record_type=record_type) for record_type in cfg.records)
    specs.append(_plain("directory", "directory"))
    specs.extend(_connector(item) for item in cfg.connectors)
    _unique(specs)
    return specs


def _plain(
    source_id: str,
    kind: SourceKind,
    *,
    folder: str | None = None,
    record_type: str | None = None,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        agent_name=agent_name(source_id),
        query_tool=tool_name("query", source_id),
        kind=kind,
        label=agent_name(source_id).replace("-", " "),
        folder=folder,
        record_type=record_type,
    )


def _connector(item: ConnectorDecl) -> SourceSpec:
    return SourceSpec(
        id=item.id,
        agent_name=agent_name(item.id),
        query_tool=tool_name("query", item.id),
        kind="composio",
        label=item.id.replace("_", " ").replace("-", " "),
        writes=item.writes,
        connector=item,
    )


def _unique(specs: list[SourceSpec]) -> None:
    names = [spec.agent_name for spec in specs]
    dupes = sorted({name for name in names if names.count(name) > 1})
    if dupes:
        raise ConfigError(f"source subagent names collide: {dupes}")
