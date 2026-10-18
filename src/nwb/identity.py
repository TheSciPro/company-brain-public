"""Who is acting. Built by the runtime (API/CLI) from the authenticated session, never from model output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Actor:
    company_id: str
    person_id: str
    asof: date
    session_id: str | None = None
    message_id: str | None = None  # the user message that triggered this turn (provenance for writes)
    run_id: str | None = None
