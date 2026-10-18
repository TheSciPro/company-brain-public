"""tiktoken encodings named in config are cached on disk and load with no network."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, TokenizerConfig
from nwb.errors import ConfigError
from nwb.kg.tokenizer import TokenizerCache
from tests.conftest import ROOT

_CHILD = """
import os
import socket
import tiktoken

original = socket.socket.connect

def _guard(self, address):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"encoding load opened a network connection to {address!r}")
    return original(self, address)

socket.socket.connect = _guard
for name in os.environ["ENCODINGS"].split(","):
    tiktoken.get_encoding(name)
"""


def test_prepare_uses_configured_names_and_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    monkeypatch.setattr("nwb.kg.tokenizer.tiktoken.get_encoding", lambda name: seen.append(name))
    cache = TokenizerCache(tmp_path, "encodings", ("cl100k_base", "o200k_base"))
    directory = cache.prepare()
    assert seen == ["cl100k_base", "o200k_base"]
    assert directory == (tmp_path / "encodings").resolve()
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(directory)


def test_tiktoken_cache_dir_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "override"))
    monkeypatch.setattr("nwb.kg.tokenizer.tiktoken.get_encoding", lambda _name: None)
    cache = TokenizerCache(tmp_path, "ignored", ("cl100k_base",))
    assert cache.prepare() == (tmp_path / "override").resolve()


def test_missing_encoding_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)

    def _boom(_name: str) -> None:
        raise OSError("offline")

    monkeypatch.setattr("nwb.kg.tokenizer.tiktoken.get_encoding", _boom)
    cache = TokenizerCache(tmp_path, "encodings", ("cl100k_base",))
    with pytest.raises(ConfigError, match="cl100k_base"):
        cache.prepare()


def test_shipped_graph_config_names_the_chunker_encoding() -> None:
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    assert cfg.tokenizer == TokenizerConfig(cache_dir="var/tiktoken", encodings=["cl100k_base"])


def test_cached_encodings_load_with_the_socket_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh interpreter loads the configured encodings from disk. The guard is on before tiktoken."""
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    cache = TokenizerCache.from_config(ROOT, cfg)
    directory = cache.prepare()
    env = os.environ.copy()
    env["TIKTOKEN_CACHE_DIR"] = str(directory)
    env["ENCODINGS"] = ",".join(cfg.tokenizer.encodings)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
