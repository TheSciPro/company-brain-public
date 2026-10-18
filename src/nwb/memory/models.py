"""Typed inputs for memory write guards. Pydantic v2, immutable.

Dated facts themselves live in ``nwb.memory.store``. This module stays the guard boundary so C2 can
extend ``WriteGuard`` without a second write schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryKind = Literal[
    "fact", "preference", "relationship", "goal", "credential", "link", "file", "correction", "outcome"
]
CaptureIntent = Literal["user_asked", "inferred_durable", "correction"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AddMemoryInput(_Frozen):
    """What a guard sees. No identity and no date: those come from the runtime."""

    content: str = Field(description="ONE durable fact, in one sentence.", min_length=3, max_length=2000)
    kind: MemoryKind = Field(
        description="fact | preference | relationship | goal | credential | link | file | correction | outcome"
    )
    mode: Literal["verbatim", "summary"] = Field(
        "summary", description="verbatim only if copied word for word from the user's latest message"
    )
    capture_intent: CaptureIntent = Field("user_asked", description="user_asked | inferred_durable | correction")
    tags: list[str] = Field(default_factory=list, max_length=8)
    people: list[str] = Field(default_factory=list, max_length=8)
    about: str | None = None
    pin: bool = False

    @field_validator("content")
    @classmethod
    def _strip(cls, v: str) -> str:
        return " ".join(v.split())
