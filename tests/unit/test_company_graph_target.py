"""Load and search must share one embedder and one FalkorDB target."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.config import Settings, get_settings
from nwb.container import Container
from nwb.errors import ConfigError
from nwb.kg.server import FalkorEndpoint, LocalFalkorServer, resolve_company_falkor
from nwb.knowledge.retrieve import UnavailableRetriever, build_company_retriever
from nwb.memory.embeddings import resolve_embedder

ROOT = Path(__file__).resolve().parents[2]


def _memory() -> MemoryConfig:
    return ConfigLoader(ROOT / "config").load("memory", MemoryConfig)


def _graph() -> GraphConfig:
    return ConfigLoader(ROOT / "config").load("graph", GraphConfig)


def _other_kind(kind: str) -> str:
    return "hashing" if kind != "hashing" else "fastembed"


def _clear_settings() -> None:
    get_settings.cache_clear()


def _load_report_type() -> Any:
    """``nwb.kg.load`` imports GraphRAG. Skip the caller when that extra is absent."""
    pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")
    from nwb.kg.load import LoadReport

    return LoadReport


def _loader_script() -> Any:
    pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")
    path = ROOT / "scripts" / "load_company_graph.py"
    spec = importlib.util.spec_from_file_location("load_company_graph_target", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loader_and_app_use_the_configured_embedder() -> None:
    """Both sides embed with memory.yaml. A hashing load is refused while the app uses fastembed."""
    cfg = _memory().embedder
    app = resolve_embedder(cfg)
    omitted = resolve_embedder(cfg, None)
    named = resolve_embedder(cfg, "config")
    assert (omitted.model, omitted.dim) == (app.model, app.dim) == (named.model, named.dim)
    assert omitted.dim == cfg.dim
    if cfg.kind == "fastembed":
        assert omitted.model == cfg.model
    with pytest.raises(ConfigError, match="does not match"):
        resolve_embedder(cfg, _other_kind(cfg.kind))


def test_loader_script_refuses_an_embedder_mismatch() -> None:
    module = _loader_script()
    cfg = _memory().embedder
    omitted = module._parser().parse_args([])
    assert omitted.embedder is None
    loaded = module._embedder(ConfigLoader(ROOT / "config"), omitted.embedder)
    assert (loaded.model, loaded.dim) == (resolve_embedder(cfg).model, resolve_embedder(cfg).dim)
    with pytest.raises(ConfigError, match="does not match"):
        module._embedder(ConfigLoader(ROOT / "config"), _other_kind(cfg.kind))


def test_empty_url_shares_the_configured_local_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """No FALKORDB_URL means graph.yaml server.local_dir, for both the resolver and search."""
    monkeypatch.setenv("FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKOR_HOST", "")
    _clear_settings()
    try:
        settings = get_settings()
        cfg = _graph()
        target = resolve_company_falkor(settings, cfg.server)
        assert target.endpoint is None
        assert target.local_dir == Path(cfg.server.local_dir)
        assert target.persist is cfg.server.persist
        assert target.local_port == cfg.server.local_port
        pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")
        seen: dict[str, Any] = {}

        def _start(self: LocalFalkorServer, timeout_s: float = 10.0) -> LocalFalkorServer:
            del timeout_s
            seen["dir"] = self.data_dir
            seen["persist"] = self.persist
            seen["port"] = self.port
            self.port = 6399
            return self

        def _build(settings: Any, cfg: Any, embedder: Any, endpoint: FalkorEndpoint) -> Any:
            del settings, cfg, embedder

            class _Stub:
                def retrieve(self, graph: str, question: str, limit: int) -> list[object]:
                    del graph, question, limit
                    seen["host"] = endpoint.host
                    seen["read_port"] = endpoint.port
                    return []

            return _Stub()

        monkeypatch.setattr(LocalFalkorServer, "start", _start)
        monkeypatch.setattr("nwb.kg.retrieve.build_sdk_retriever", _build)
        retriever = build_company_retriever(settings, cfg, resolve_embedder(_memory().embedder))
        assert not isinstance(retriever, UnavailableRetriever)
        assert retriever.retrieve("acme_kb_public", "handbook", 1) == []
        assert seen["dir"] == Path(cfg.server.local_dir)
        assert seen["persist"] is cfg.server.persist
        assert seen["port"] == cfg.server.local_port
        assert seen["host"] == "127.0.0.1"
        assert seen["read_port"] == 6399
    finally:
        _clear_settings()


def test_loader_writes_the_directory_search_reads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The loader's server directory is the configured local dir, not a temp folder."""
    module = _loader_script()
    load_report = _load_report_type()
    monkeypatch.setenv("FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKOR_HOST", "")
    _clear_settings()
    seen: dict[str, Any] = {}

    @contextmanager
    def _open(target: Any) -> Iterator[FalkorEndpoint]:
        seen["dir"] = target.local_dir
        seen["persist"] = target.persist
        yield FalkorEndpoint(host="127.0.0.1", port=6399)

    async def _load(*args: Any, **kwargs: Any) -> Any:
        del args
        seen["endpoint"] = kwargs["endpoint"]
        seen["work"] = kwargs["work_dir"]
        return load_report(company=kwargs["company"], prefix=kwargs["prefix"], embedder=kwargs["embedder"].model)

    monkeypatch.setattr(module, "open_company_falkor", _open)
    monkeypatch.setattr(module, "load_company", _load)
    try:
        cfg = _graph()
        args = module._parser().parse_args(
            ["--llm", "offline", "--tables-only", "--cassette-dir", str(tmp_path / "cassettes")]
        )
        report = asyncio.run(module._run(ROOT, args, "acme", ""))
        assert seen["dir"] == Path(cfg.server.local_dir)
        assert seen["persist"] is cfg.server.persist
        assert "tmp" not in Path(seen["dir"]).parts
        assert seen["work"] != seen["dir"]
        assert seen["endpoint"].host == "127.0.0.1"
        assert report["embedder"] == resolve_embedder(_memory().embedder).model
    finally:
        _clear_settings()


def test_falkor_host_is_shared_when_the_url_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``falkor_target()`` names the server when no URL is set and a host is configured."""
    monkeypatch.setenv("FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKOR_HOST", "127.0.0.1")
    monkeypatch.setenv("NWB_FALKOR_PORT", "6391")
    _clear_settings()
    try:
        settings = get_settings()
        cfg = _graph()
        target = resolve_company_falkor(settings, cfg.server)
        remote = settings.falkor_target()
        assert target.local_dir is None
        assert target.endpoint is not None
        assert (target.endpoint.host, target.endpoint.port) == (remote.host, remote.port) == ("127.0.0.1", 6391)
        pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")
        seen: dict[str, Any] = {}

        def _start(self: LocalFalkorServer, timeout_s: float = 10.0) -> LocalFalkorServer:
            del timeout_s
            seen["started"] = True
            return self

        def _build(settings: Any, cfg: Any, embedder: Any, endpoint: FalkorEndpoint) -> Any:
            del settings, cfg, embedder

            class _Stub:
                def retrieve(self, graph: str, question: str, limit: int) -> list[object]:
                    del graph, question, limit
                    seen["endpoint"] = (endpoint.host, endpoint.port)
                    return []

            return _Stub()

        monkeypatch.setattr(LocalFalkorServer, "start", _start)
        monkeypatch.setattr("nwb.kg.retrieve.build_sdk_retriever", _build)
        retriever = build_company_retriever(settings, cfg, resolve_embedder(_memory().embedder))
        assert not isinstance(retriever, UnavailableRetriever)
        retriever.retrieve("acme_kb_public", "handbook", 1)
        assert "started" not in seen
        assert seen["endpoint"] == ("127.0.0.1", 6391)
    finally:
        _clear_settings()


def test_url_endpoint_is_shared_and_does_not_start_a_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FALKORDB_URL", "redis://localhost:6379")
    monkeypatch.setenv("NWB_FALKORDB_URL", "")
    _clear_settings()
    try:
        settings = get_settings()
        cfg = _graph()
        target = resolve_company_falkor(settings, cfg.server)
        assert target.local_dir is None
        assert target.endpoint is not None
        assert (target.endpoint.host, target.endpoint.port) == ("localhost", 6379)
        pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")
        started = {"n": 0}

        def _start(self: LocalFalkorServer, timeout_s: float = 10.0) -> LocalFalkorServer:
            del timeout_s
            started["n"] += 1
            return self

        def _build(settings: Any, cfg: Any, embedder: Any, endpoint: FalkorEndpoint) -> Any:
            del settings, cfg, embedder

            class _Stub:
                def retrieve(self, graph: str, question: str, limit: int) -> list[object]:
                    del graph, question, limit
                    started["endpoint"] = (endpoint.host, endpoint.port)
                    return []

            return _Stub()

        monkeypatch.setattr(LocalFalkorServer, "start", _start)
        monkeypatch.setattr("nwb.kg.retrieve.build_sdk_retriever", _build)
        retriever = build_company_retriever(settings, cfg, resolve_embedder(_memory().embedder))
        assert not isinstance(retriever, UnavailableRetriever)
        retriever.retrieve("acme_kb_public", "handbook", 1)
        assert started["n"] == 0
        assert started["endpoint"] == ("localhost", 6379)
    finally:
        _clear_settings()


def test_local_flag_keeps_the_configured_dir_when_a_url_is_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _loader_script()
    load_report = _load_report_type()
    monkeypatch.setenv("FALKORDB_URL", "redis://localhost:6379")
    _clear_settings()
    seen: dict[str, Any] = {}

    @contextmanager
    def _open(target: Any) -> Iterator[FalkorEndpoint]:
        seen["dir"] = target.local_dir
        seen["endpoint"] = target.endpoint
        yield FalkorEndpoint(host="127.0.0.1", port=6399)

    async def _load(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return load_report(company="acme", prefix="")

    monkeypatch.setattr(module, "open_company_falkor", _open)
    monkeypatch.setattr(module, "load_company", _load)
    try:
        cfg = _graph()
        forced = resolve_company_falkor(get_settings(), cfg.server, force_local=True)
        args = module._parser().parse_args(
            ["--llm", "offline", "--tables-only", "--local", "--cassette-dir", str(tmp_path / "cassettes")]
        )
        asyncio.run(module._run(ROOT, args, "acme", ""))
        assert forced.local_dir == Path(cfg.server.local_dir)
        assert seen["dir"] == forced.local_dir
        assert seen["endpoint"] is None
    finally:
        _clear_settings()


def test_fact_book_opens_the_company_graph_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fact graphs use ``server.local_dir``, not the embedded file beside SQLite, and search reuses that server."""
    monkeypatch.setenv("FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKOR_HOST", "")
    _clear_settings()
    seen: dict[str, Any] = {}

    class _Client:
        def __init__(self, host: str = "localhost", port: int = 6379, **kwargs: Any) -> None:
            del kwargs
            seen["client"] = (host, port)

        def close(self) -> None:
            seen["closed"] = True

    def _start(self: LocalFalkorServer, timeout_s: float = 10.0) -> LocalFalkorServer:
        del timeout_s
        seen["dir"] = self.data_dir
        seen["persist"] = self.persist
        self.host = "127.0.0.1"
        self.port = 6401
        return self

    def _build(settings: Any, cfg: Any, embedder: Any, endpoint: Any = None) -> Any:
        del settings, cfg, embedder
        seen["search"] = None if endpoint is None else (endpoint.host, endpoint.port)
        return UnavailableRetriever()

    monkeypatch.setattr("nwb.container.FalkorServer", _Client)
    monkeypatch.setattr(LocalFalkorServer, "start", _start)
    monkeypatch.setattr("nwb.container.build_company_retriever", _build)

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sqlite_path=tmp_path / "t.sqlite",
        config_dir=ROOT / "config",
        falkor_lite_path=tmp_path / "org.falkor",
    )
    container = Container.from_settings(settings)
    server = container.graph_config.server.model_copy(update={"local_dir": str(tmp_path / "graphs")})
    container.graph_config = container.graph_config.model_copy(update={"server": server})
    try:
        assert container.fact_book is not None
        assert container.company_search is not None
        assert seen["dir"] == tmp_path / "graphs"
        assert seen["persist"] is server.persist
        assert seen["dir"] != settings.falkor_lite_path
        assert seen["client"] == ("127.0.0.1", 6401)
        assert seen["search"] == ("127.0.0.1", 6401)
        assert container.embedder.model == resolve_embedder(_memory().embedder).model
        assert container.embedder.dim == resolve_embedder(_memory().embedder).dim
    finally:
        container.close()
        _clear_settings()


def test_fact_book_uses_falkor_target_when_a_host_is_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKORDB_URL", "")
    monkeypatch.setenv("NWB_FALKOR_HOST", "127.0.0.1")
    monkeypatch.setenv("NWB_FALKOR_PORT", "6391")
    _clear_settings()
    seen: dict[str, Any] = {}

    class _Client:
        def __init__(self, host: str = "localhost", port: int = 6379, **kwargs: Any) -> None:
            del kwargs
            seen["client"] = (host, port)

        def close(self) -> None:
            return None

    def _start(self: LocalFalkorServer, timeout_s: float = 10.0) -> LocalFalkorServer:
        del timeout_s
        seen["started"] = True
        return self

    monkeypatch.setattr("nwb.container.FalkorServer", _Client)
    monkeypatch.setattr(LocalFalkorServer, "start", _start)

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        sqlite_path=tmp_path / "t.sqlite",
        config_dir=ROOT / "config",
    )
    container = Container.from_settings(settings)
    try:
        assert container.fact_book is not None
        assert "started" not in seen
        assert seen["client"] == ("127.0.0.1", 6391)
    finally:
        container.close()
        _clear_settings()
