"""The company-graph loader and the app must name the same FalkorDB graphs."""

from __future__ import annotations

from pathlib import Path

import pytest

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig
from nwb.config import get_settings, graph_name_prefix
from nwb.ingest.acl import classification_config

ROOT = Path(__file__).resolve().parents[2]


def _level() -> str:
    return classification_config().levels[0].code


def test_default_prefix_matches_the_runtime_graph_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """No flag and an empty GRAPH_PREFIX name ``{company}_kb_{level}``, not ``local_<random>_``."""
    monkeypatch.setenv("GRAPH_PREFIX", "")
    monkeypatch.setenv("NWB_GRAPH_PREFIX", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
        prefix = graph_name_prefix(None, settings.graph_prefix)
        company = settings.company_id
        level = _level()
        loaded = cfg.company_graph(company, level, prefix=prefix)
        runtime = cfg.company_graph(company, level, prefix=settings.graph_prefix)
        assert prefix == ""
        assert loaded == runtime
        assert loaded == cfg.company_graph(company, level, prefix="")
        assert not loaded.startswith("local_")
    finally:
        get_settings.cache_clear()


def test_configured_prefix_is_the_one_the_app_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting --prefix uses GRAPH_PREFIX. Passing an empty --prefix forces no prefix."""
    monkeypatch.setenv("GRAPH_PREFIX", "shared")
    monkeypatch.setenv("NWB_GRAPH_PREFIX", "shared")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
        level = _level()
        company = settings.company_id
        prefix = graph_name_prefix(None, settings.graph_prefix)
        assert prefix == settings.graph_prefix
        assert cfg.company_graph(company, level, prefix=prefix) == cfg.company_graph(
            company, level, prefix=settings.graph_prefix
        )
        assert graph_name_prefix("", settings.graph_prefix) == ""
        assert cfg.company_graph(company, level, prefix="") == cfg.company_graph(company, level)
    finally:
        get_settings.cache_clear()
