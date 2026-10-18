"""Request/response bodies. Identity is never part of a body; it comes from the IdentityProvider."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartSessionRequest(_Body):
    asof: date | None = Field(None, description="ask date; defaults to the persona date, else today")


class SessionResponse(BaseModel):
    session_id: str
    person_id: str
    asof: date


class ChatRequest(_Body):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(None, description="omit to start a new session")
    asof: date | None = None


class ResumeRequest(_Body):
    decisions: list[dict[str, Any]] = Field(min_length=1, description='e.g. [{"type": "approve"}]')


class DecideRequest(_Body):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(None, max_length=300)


class PersonaOut(BaseModel):
    id: str
    label: str
    asof: date | None


class InboxItemOut(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    ref: str | None
    created_at: str
    read: bool
