"""Action and approval views."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Decision = Literal["approve", "reject"]


class ActionView(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: str
    approval_id: str | None
    kind: str
    title: str
    status: str
    requester: str
    approver: str | None
    target: str | None
    args: dict[str, Any]
    proposed_by: str
    note: str | None = None
