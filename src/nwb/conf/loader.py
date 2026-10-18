"""Load and validate ``config/<name>.yaml`` into its pydantic model, cached per loader instance."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from nwb.errors import ConfigError

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ReloadReport:
    """Which config files were re-read because their bytes changed, and which were left cached."""

    reloaded: tuple[str, ...]
    unchanged: tuple[str, ...]


class ConfigLoader:
    """One loader per process (owned by the container). ``load`` is cached by (file, model).

    ``refresh`` compares file bytes to the last digest. A changed file drops its cache so the next
    ``load`` reparses it; an unchanged file is not read again. The first time a name is seen it is
    recorded as unchanged: there was no previous copy to reload.
    """

    def __init__(self, config_dir: Path) -> None:
        self._dir = config_dir
        self._cache: dict[tuple[str, type[BaseModel]], BaseModel] = {}
        self._digest: dict[str, str] = {}

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, name: str) -> Path:
        path = self._dir / f"{name}.yaml"
        if not path.is_file():
            raise ConfigError(f"missing config file {path}")
        return path

    def raw(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        blob = path.read_bytes()
        self._digest[name] = hashlib.sha256(blob).hexdigest()
        data = yaml.safe_load(blob.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a mapping at the top level")
        return data

    def load(self, name: str, model: type[M]) -> M:
        key = (name, model)
        if key not in self._cache:
            try:
                self._cache[key] = model.model_validate(self.raw(name))
            except ValidationError as exc:
                raise ConfigError(f"invalid {self._dir / name}.yaml: {exc}") from exc
        cached = self._cache[key]
        assert isinstance(cached, model)
        return cached

    def refresh(self, names: Sequence[str]) -> ReloadReport:
        """Drop the cache for names whose file bytes changed. Unchanged names stay cached."""
        reloaded: list[str] = []
        unchanged: list[str] = []
        for name in dict.fromkeys(names):
            digest = hashlib.sha256(self._path(name).read_bytes()).hexdigest()
            prev = self._digest.get(name)
            if prev is None or prev == digest:
                self._digest[name] = digest
                unchanged.append(name)
                continue
            self._cache = {key: value for key, value in self._cache.items() if key[0] != name}
            self._digest[name] = digest
            reloaded.append(name)
        return ReloadReport(tuple(reloaded), tuple(unchanged))
