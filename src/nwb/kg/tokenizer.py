"""Local tiktoken encodings for GraphRAG's prose chunker.

GraphRAG calls ``tiktoken.get_encoding`` while it chunks a document. tiktoken's default is to download the
encoding into a temp directory on first use. A cold machine, a fresh container, and the offline replay test
all fail that download. ``TokenizerCache.prepare`` writes the encodings named in ``config/graph.yaml`` into
``tokenizer.cache_dir`` (or ``TIKTOKEN_CACHE_DIR`` when that is set) and points tiktoken at that directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from nwb.conf.models import GraphConfig
from nwb.errors import ConfigError
from nwb.obs.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenizerCache:
    """One cache directory and the encoding names the chunker is allowed to load."""

    root: Path
    cache_dir: str
    encodings: tuple[str, ...]

    @classmethod
    def from_config(cls, root: Path, cfg: GraphConfig) -> TokenizerCache:
        return cls(root, cfg.tokenizer.cache_dir, tuple(cfg.tokenizer.encodings))

    def directory(self) -> Path:
        """Absolute cache path. An already-set ``TIKTOKEN_CACHE_DIR`` wins over the config path."""
        override = os.environ.get("TIKTOKEN_CACHE_DIR", "").strip()
        if override:
            path = Path(override)
        else:
            path = Path(self.cache_dir)
            if not path.is_absolute():
                path = self.root / path
        return path.resolve()

    def prepare(self) -> Path:
        """Download any missing encoding into the cache, then keep later loads on that directory."""
        directory = self.directory()
        directory.mkdir(parents=True, exist_ok=True)
        os.environ["TIKTOKEN_CACHE_DIR"] = str(directory)
        for name in self.encodings:
            try:
                tiktoken.get_encoding(name)
            except Exception as exc:
                raise ConfigError(
                    f"tiktoken encoding {name!r} is not in {directory}. "
                    "Run `nwb cache-tokenizers` where outbound HTTPS is allowed, then retry offline."
                ) from exc
        log.info("tiktoken cache ready", directory=str(directory), encodings=list(self.encodings))
        return directory
