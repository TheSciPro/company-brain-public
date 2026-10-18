"""Typed value objects for Layer 1 (org, people, groups, access). Immutable; validated at the boundary."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

Classification = Literal["public", "internal", "confidential", "highly_confidential"]
ShareScope = Literal["team", "leadership"]
SubjectType = Literal["person", "group"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Company(_Frozen):
    id: str
    name: str


class OrgUnit(_Frozen):
    id: str
    company_id: str
    kind: Literal["department", "team"]
    name: str
    parent_id: str | None = None
    dept_id: str | None = None
    valid_from: date = date(2000, 1, 1)
    valid_to: date | None = None
    src_id: str | None = None


class Person(_Frozen):
    id: str
    company_id: str
    name: str
    email: str | None = None
    title: str | None = None
    grade: str | None = None
    location: str | None = None
    country: str | None = None
    status: Literal["active", "left", "suspended"] = "active"
    external: bool = False
    hired_on: date | None = None
    left_on: date | None = None
    src_id: str | None = None


class _Dated(_Frozen):
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def _order(self) -> _Dated:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError(f"valid_to {self.valid_to} < valid_from {self.valid_from}")
        return self

    def active_on(self, d: date) -> bool:
        return self.valid_from <= d and (self.valid_to is None or self.valid_to >= d)


class Membership(_Dated):
    person_id: str
    unit_id: str
    role: str | None = None
    is_leader: bool = False
    is_primary: bool = True
    src_id: str | None = None

    @property
    def id(self) -> str:
        return f"{self.person_id}:{self.unit_id}:{self.valid_from.isoformat()}"


class ReportingLine(_Dated):
    person_id: str
    manager_id: str
    src_id: str | None = None


class Group(_Frozen):
    id: str
    company_id: str
    name: str
    src_id: str | None = None


class GroupMember(_Dated):
    group_id: str
    person_id: str


class AccessRule(_Frozen):
    id: str
    company_id: str
    source_tag: str
    classification: Classification
    share_scope: ShareScope | None = None
    subjects: tuple[tuple[SubjectType, str], ...] = ()


class Grant(_Dated):
    subject_type: Literal["person", "group", "unit"]
    subject_id: str
    resource_type: Literal["record", "document", "project"]
    resource_id: str
    relation: Literal["reader", "existence", "editor"] = "reader"

    @property
    def id(self) -> str:
        return f"{self.resource_type}:{self.resource_id}:{self.subject_type}:{self.subject_id}"


class _Content(_Frozen):
    id: str
    company_id: str
    owner_person_id: str | None = None
    owner_unit_id: str | None = None
    dept_id: str | None = None
    classification: Classification
    share_scope: ShareScope | None = None
    source_tag: str | None = None
    personal: bool = False
    hash: str

    @model_validator(mode="after")
    def _share(self) -> _Content:
        if self.classification == "confidential" and self.share_scope is None:
            raise ValueError("confidential content needs a share_scope")
        if self.personal and not self.owner_person_id:
            raise ValueError("personal content needs an owner")
        return self


class Record(_Content):
    source: str
    source_id: str
    record_type: str
    payload: str
    occurred_at: str | None = None


class Document(_Content):
    title: str
    doc_type: str | None = None
    version: int = 1
    blob_url: str
    mime: str | None = "text/markdown"
    bytes: int | None = None
    source: str
    source_id: str
