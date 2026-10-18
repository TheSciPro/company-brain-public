"""Connector ports. A connector does two narrow things, and both go through existing guards:

* ``SourceConnector`` reads items (mail, messages, files) as ``SourceItem`` with ACL principals, so the Layer-2
  loader can store them as documents and map source ACLs to ``grants``. The quote guard and the precedence engine
  still decide what becomes a fact.
* ``ActionSink`` executes an *approved* action (``nwb.actions``) in the external app. The approval queue stays
  the only door; a sink never sees unapproved actions.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["person", "group", "unit"]
    ref: str  # an email or a group name in the source; resolved to NW ids by the loader


class SourceItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str  # e.g. "gmail", "slack", "gdrive"
    external_id: str
    title: str
    body: str
    author: str | None = None
    created_at: datetime
    readers: tuple[Principal, ...] = Field(default=(), description="who can read it in the source (ACL)")
    url: str | None = None


class SourceConnector(Protocol):
    name: str

    def items(self, since: datetime | None) -> Iterator[SourceItem]: ...


class ActionSink(Protocol):
    name: str

    def execute(self, kind: str, target_ref: str, title: str, message: str) -> str | None:
        """Run an approved action; returns the external reference (message id, ticket id), or None if none came back."""
        ...


class ComposioClient(Protocol):
    """Injected network client. The connector never reads an API key and never imports the SDK."""

    def execute(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...
