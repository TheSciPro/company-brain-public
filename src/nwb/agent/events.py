"""Typed chat events streamed to the CLI and the API (SSE). One schema for both."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal["run_started", "token", "tool_start", "tool_end", "receipt", "interrupt", "final", "error"]


class ChatEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: EventType
    run_id: str
    data: dict[str, Any] = Field(default_factory=dict)


class TurnResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    run_id: str
    session_id: str
    answer: str
    status: Literal["done", "failed", "interrupted"]
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    interrupt: dict[str, Any] | None = None
    error: str | None = None
