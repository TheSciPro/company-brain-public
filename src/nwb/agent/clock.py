"""Clock port so time is injectable (tests freeze it; prod uses the system clock in the configured timezone)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def __init__(self, tz: str) -> None:
        self._tz = ZoneInfo(tz)

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


def resolve_asof(explicit: date | None, default: date | None, clock: Clock) -> date:
    return explicit or default or clock.now().date()
