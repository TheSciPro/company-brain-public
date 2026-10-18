"""Embedder port + adapters. fastembed (local ONNX, no key, no quota) or a hashing embedder for tests/offline."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from nwb.conf.models import EmbedderConfig
from nwb.errors import ConfigError


class Embedder(Protocol):
    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=False))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


class HashingEmbedder:
    """Deterministic bag-of-words hashing (signed). Good enough for tests and as an offline fallback."""

    model = "hashing-v1"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in re.findall(r"[a-z0-9]+", t.lower()):
                h = int(hashlib.blake2b(tok.encode(), digest_size=8).hexdigest(), 16)
                v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class FastEmbedEmbedder:
    """Lazy-loads the ONNX model on first use (downloads once over HTTPS, then cached)."""

    def __init__(self, model: str, dim: int) -> None:
        self.model = model
        self.dim = dim
        self._impl: Any = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._impl is None:
            from fastembed import TextEmbedding

            self._impl = TextEmbedding(self.model)
        return [[float(x) for x in v] for v in self._impl.embed(list(texts))]


def build_embedder(cfg: EmbedderConfig) -> Embedder:
    if cfg.kind == "hashing":
        return HashingEmbedder(cfg.dim)
    return FastEmbedEmbedder(cfg.model, cfg.dim)


def resolve_embedder(cfg: EmbedderConfig, choice: str | None = None) -> Embedder:
    """The embedder named by one ``EmbedderConfig`` (``memory.yaml``).

    The company-graph loader and the running app both call this, so a load and a
    search share a vector space. ``choice`` is the loader's ``--embedder`` flag.
    Omitted, blank, or ``config`` means ``cfg``. Naming a kind is allowed only
    when it is ``cfg.kind``; a hashing load is refused while the app embeds with
    the configured model, even when both vectors have the same dimension.
    """
    selected = "config" if choice is None else choice.strip()
    if selected in {"", "config"} or selected == cfg.kind:
        return build_embedder(cfg)
    raise ConfigError(
        f"embedder {selected!r} does not match the configured embedder {cfg.kind!r} ({cfg.model}, dim {cfg.dim})"
    )
