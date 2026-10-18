"""Write guards (open/closed: add a guard class, inject it). Each returns a rejection reason or None.

The quality gate runs on every memory write: episode chunks and ``/memories/facts/`` writes go through
``Container.write_guards`` (``QualityGuard``), and ``USER.md`` / ``MEMORY.md`` go through ``MemoryWriteGate``
inside ``MemoriesBackend``. Categories and thresholds come from ``config/memory.yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Protocol

from nwb.conf.models import QualityConfig, QualityRule
from nwb.memory.models import AddMemoryInput
from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)


def normalise(text: str) -> str:
    return " ".join(text.lower().split())


class WriteGuard(Protocol):
    def check(self, inp: AddMemoryInput, source_text: str | None) -> str | None: ...


class LengthGuard:
    def __init__(self, max_chars: int) -> None:
        self._max = max_chars

    def check(self, inp: AddMemoryInput, source_text: str | None) -> str | None:
        return f"content longer than {self._max} chars; save one fact" if len(inp.content) > self._max else None


class SecretGuard:
    """Credentials are stored as hints only (AGENTS 8.3): anything that looks like a secret is rejected."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns = [re.compile(p) for p in patterns]

    def reject_text(self, text: str) -> str | None:
        if any(p.search(text) for p in self._patterns):
            return "looks like a secret (password/token/card); store only a hint such as where it is kept"
        return None

    def check(self, inp: AddMemoryInput, source_text: str | None) -> str | None:
        return self.reject_text(inp.content)


class QuoteGuard:
    """``mode=verbatim`` must be an exact (whitespace/case-normalised) substring of the provenance message."""

    def check(self, inp: AddMemoryInput, source_text: str | None) -> str | None:
        if inp.mode != "verbatim":
            return None
        if not source_text or normalise(inp.content) not in normalise(source_text):
            return "verbatim content does not appear in the user's message; use mode=summary"
        return None


class _CompiledRule:
    def __init__(self, name: str, rule: QualityRule) -> None:
        self.name = name
        self.rule = rule
        self.patterns = [re.compile(p) for p in rule.patterns]

    def hits(self, text: str) -> int:
        return sum(len(p.findall(text)) for p in self.patterns)


class QualityGate:
    """Reject venting, self-corrections, stack traces and pasted error dumps. Rules come from config."""

    def __init__(self, cfg: QualityConfig) -> None:
        self._cfg = cfg
        self._rules = [_CompiledRule(name, rule) for name, rule in cfg.rules.items()]

    @property
    def guarded_files(self) -> frozenset[str]:
        return frozenset(self._cfg.guarded_files)

    def message(self, category: str, reason: str) -> str:
        base = f"rejected ({category}): {reason}"
        suffix = self._cfg.rejection_suffix.strip()
        return f"{base}. {suffix}" if suffix else base

    def reject(self, text: str) -> str | None:
        """Return the agent-facing reason, or None when the text may be stored."""
        if not text or not text.strip():
            return None
        for rule in self._rules:
            if len(text) < rule.rule.min_chars:
                continue
            if rule.hits(text) >= rule.rule.min_matches:
                return self.message(rule.name, rule.rule.reason)
        return None


class QualityGuard:
    """``WriteGuard`` so episode chunks and fact-file writes run the quality gate."""

    def __init__(self, gate: QualityGate) -> None:
        self._gate = gate

    def check(self, inp: AddMemoryInput, source_text: str | None) -> str | None:
        reason = self._gate.reject(inp.content)
        if reason:
            _log_rejection(reason)
        return reason


class MemoryWriteGate:
    """Secret guard plus quality gate for ``USER.md`` / ``MEMORY.md``.

    Only the configured filenames are checked. Other notes under ``/memories/`` are left alone.
    ``LengthGuard`` is not applied here: a profile is longer than one fact.
    """

    def __init__(self, secrets: SecretGuard, quality: QualityGate) -> None:
        self._secrets = secrets
        self._quality = quality

    def applies(self, path: str) -> bool:
        return PurePosixPath(path).name in self._quality.guarded_files

    def reject_text(self, text: str) -> str | None:
        if secret := self._secrets.reject_text(text):
            return self._quality.message("secret", secret)
        return self._quality.reject(text)

    def reject(self, path: str, text: str) -> str | None:
        if not self.applies(path):
            return None
        reason = self.reject_text(text)
        if reason:
            _log_rejection(reason, path=path)
        return reason


def _log_rejection(reason: str, path: str | None = None) -> None:
    """Log the category, never the text that was refused."""
    log.info("memory write rejected", path=path, reason=reason)
    dataflow(Hop.MEMORY_WRITE, event="REJECTED", path=path, reason=reason)
