"""Onboarding outputs returned to tools, the API and the heartbeat."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ItemStatus = Literal["todo", "doing", "done", "waived", "blocked"]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class BriefItem(_Out):
    id: str
    title: str
    category: str
    due_day: int
    due_date: date
    status: str
    overdue: bool
    mandatory: bool
    ref: str | None = None


class OnboardingBrief(_Out):
    person_id: str
    name: str
    day: int
    phase: str
    start_date: date
    manager: str | None
    buddy: str | None
    progress: str = Field(description="done/total mandatory items")
    overdue: list[BriefItem]
    upcoming: list[BriefItem]


class ItemUpdate(_Out):
    item_id: str
    status: ItemStatus
    ok: bool
    note: str | None = None


class OnboardingStep(_Out):
    """What ``onboarding_step`` tells the model after the state machine moves."""

    beat: str | None = None
    phase: str
    next_beat: str | None = None
    skipped: bool = False
    completed: bool = False
    error: str | None = None
