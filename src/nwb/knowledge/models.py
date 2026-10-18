"""Response envelopes for knowledge tools. Every answer carries the citations it actually used."""

from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializationInfo, SerializerFunctionWrapHandler, model_serializer

AUTHORITY_LABELS = {
    5: "contract",
    4: "approved exception",
    3: "approved policy",
    2: "minutes/decision",
    1: "draft/chat",
}


FOR_MODEL = {"for_model": True}  # serialization context: the JSON the LLM reads, not the UI/audit artifact


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)
    # Fields kept in the artifact (audit, UI evidence) but left out of what the model reads. The model echoed
    # audit ids ("dec-…") into answers, and graph provenance would only cost tokens.
    _audit_only: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _drop_audit_only(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo) -> Any:
        data = handler(self)
        if isinstance(data, dict) and info.context == FOR_MODEL:
            for name in self._audit_only:
                data.pop(name, None)
        return data


class Citation(_Out):
    """One source the answer rests on. ``id`` is what the model puts in square brackets."""

    id: str
    kind: Literal["fact", "document", "record", "person"]
    title: str | None = None
    quote: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    authority: str | None = None
    recorded_at: int | None = Field(None, description="epoch ms when this source was learned")
    level: str | None = Field(None, description="classification graph the source was read from")
    graph: str | None = None
    node_id: str | None = None
    node_label: str | None = None
    query: str | None = None

    _audit_only: ClassVar[frozenset[str]] = frozenset({"graph", "node_id", "node_label", "query"})


class FactValue(_Out):
    value: str
    unit: str | None = None
    status: str
    scope: str | None = None
    withdrawn: bool = Field(False, description="closed by a later fact; still the answer for an earlier date")
    citation: Citation


class KeyAnswer(_Out):
    """The as-of answer for one discovered fact key, resolved for the asker's scopes (client, country, ...)."""

    key: str
    label: str | None = None
    score: float = Field(0.0, description="how well the key matches the question (0..1)")
    asof: date
    scopes_tried: list[str] = Field(default_factory=list, description="most specific first; '' = company-wide")
    answer: FactValue | None
    overridden_general: FactValue | None = Field(None, description="the less specific rule the answer beats")
    replaced: FactValue | None = Field(None, description="the previous value this one superseded")
    contested: list[FactValue] = Field(default_factory=list, description="drafts/conflicts: report, never use")
    note: str | None = None


class FactLookup(_Out):
    """``company_fact``: a question resolved to the best-matching fact keys found in the data.

    ``say`` is the sentence to tell the user. ``stop`` means do not call another tool.
    """

    question: str
    say: str | None = None
    stop: bool = False
    client: str | None = None
    candidates: list[KeyAnswer] = Field(default_factory=list, description="best match first")
    decision_id: str | None = None
    note: str | None = None

    _audit_only: ClassVar[frozenset[str]] = frozenset({"decision_id"})

    @property
    def best(self) -> KeyAnswer | None:
        return next((c for c in self.candidates if c.answer is not None), None)


class FactVersion(_Out):
    value: str
    unit: str | None
    status: str
    scope: str | None
    valid_from: date
    valid_to: date | None
    recorded_at: int | None = None
    withdrawn: bool = False
    citation: Citation


class FactHistory(_Out):
    key: str
    label: str | None
    versions: list[FactVersion]
    note: str | None = None


class DoaRule(_Out):
    rule_id: str
    action: str
    region: str
    min_amount: float | None
    max_amount: float | None
    unit: str | None
    approver_phrase: str
    valid_from: date | None = None


class ApproverAnswer(_Out):
    requester: str
    asof: date
    rule: DoaRule | None
    approver: Citation | None = None
    chain: list[str] = Field(default_factory=list, description="reporting chain walked, nearest first")
    candidates: list[str] = Field(default_factory=list, description="rule actions to choose from if ambiguous")
    citations: list[Citation] = Field(default_factory=list)
    decision_id: str | None = None
    note: str | None = None

    _audit_only: ClassVar[frozenset[str]] = frozenset({"decision_id"})


class CompanyHit(_Out):
    id: str
    kind: Literal["document", "record", "graph"]
    title: str
    snippet: str
    classification: str | None = None
    date: str | None = None
    score: float
    graph: str | None = None

    _audit_only: ClassVar[frozenset[str]] = frozenset({"graph"})


class NotSearched(_Out):
    """A classification graph that was not opened. ``reason`` is why, never a count."""

    source: str
    reason: Literal["access", "audience", "empty", "unavailable"]


class CompanySearchResult(_Out):
    """Passages actually read from the graphs this caller may query.

    ``hidden_matches`` is always zero and is not serialised. The tool payload
    has no count of items the asker cannot see.
    """

    query: str
    hits: list[CompanyHit]
    searched: list[str] = Field(default_factory=list)
    not_searched: list[NotSearched] = Field(default_factory=list)
    note: str | None = None
    say: str | None = Field(None, description="set when nothing was found: the reply to give, as is")
    stop: bool | None = None

    @property
    def hidden_matches(self) -> int:
        return 0


class PersonCard(_Out):
    id: str
    name: str
    title: str | None
    grade: str | None
    location: str | None
    manager: str | None
    units: list[str]
    asof: date
