"""Eval case files (YAML). The values asserted are test data; the runner is generic."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PersonaSpec(_Frozen):
    """A person by id, or the first active employee matching a country and whether they have a client."""

    id: str | None = None
    country: str | None = None
    with_client: bool | None = None

    @model_validator(mode="after")
    def _one_way(self) -> PersonaSpec:
        if self.id is None and self.country is None:
            raise ValueError("persona needs an id or a country")
        return self


class Expect(_Frozen):
    value: str | None = None  # a tool artifact must carry this value, and the answer must say it
    cite: str | None = None  # a tool artifact id must start with this, and the answer must cite it
    memory_file: str | None = None  # after the run, this /memories/ file must contain `contains`
    contains: str | None = None
    tools: tuple[str, ...] = ()  # tools that must have been called


class EvalCase(_Frozen):
    id: str
    persona: PersonaSpec
    ask: str | list[str]
    expect: Expect
    heldout: bool = False
    asof: date | None = None

    @property
    def turns(self) -> list[str]:
        return [self.ask] if isinstance(self.ask, str) else list(self.ask)


class CaseFile(_Frozen):
    version: int = 1
    asof: date | None = None
    cases: list[EvalCase] = Field(default_factory=list)


def load_cases(path: Path) -> list[EvalCase]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    f = CaseFile.model_validate(raw)
    return [c if c.asof or not f.asof else c.model_copy(update={"asof": f.asof}) for c in f.cases]
