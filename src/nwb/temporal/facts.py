"""Fact value objects and the authority scale."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date
from enum import IntEnum
from typing import Literal

FactStatus = Literal["current", "superseded", "conflict", "draft", "corroborates"]


class Authority(IntEnum):
    """contract 5 > approved exception 4 > approved policy 3 > minutes/decision 2 > chat/draft 1."""

    CHAT_OR_DRAFT = 1
    MINUTES_OR_DECISION = 2
    APPROVED_POLICY = 3
    APPROVED_EXCEPTION = 4
    CONTRACT = 5


def normalise_value(v: object) -> str:
    """'18', 18, 18.0 -> '18' so equal values compare equal."""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, int | float):
        return f"{v:g}"
    s = str(v).strip()
    try:
        return f"{float(s):g}"
    except ValueError:
        return s


@dataclass(frozen=True, slots=True)
class Fact:
    key: str
    about: str
    value: str
    valid_from: date
    authority: Authority
    src_type: Literal["record", "document"]
    src_id: str
    quote: str
    company_id: str = "NW"
    scope: str | None = None
    unit: str | None = None
    draft: bool = False
    valid_to: date | None = None
    status: FactStatus = "current"
    supersedes_id: str | None = None
    overrides_id: str | None = None
    related_id: str | None = None
    label: str | None = None  # human description of the key (keys are discovered from data)
    title: str | None = None  # source document title, stored on the graph node for citations
    level: str | None = None  # classification graph this fact lives in
    recorded_at: int | None = None  # epoch ms when we learned it (the second clock)
    # Read provenance, for the UI's evidence panel: the FalkorDB graph, node label and query that returned it.
    graph: str | None = field(default=None, compare=False)
    node_label: str | None = field(default=None, compare=False)
    query: str | None = field(default=None, compare=False)

    @property
    def id(self) -> str:
        raw = "|".join([self.key, self.about, self.scope or "", self.src_id, self.value])
        return "F-" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    def with_(self, **changes: object) -> Fact:
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FactAnswer:
    """As-of answer: the winning fact plus what it overrode/replaced and anything contested."""

    fact: Fact | None
    overridden: Fact | None = None
    replaced: Fact | None = None
    contested: tuple[Fact, ...] = ()
