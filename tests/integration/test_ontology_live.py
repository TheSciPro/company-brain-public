"""One live ontology discovery when NVIDIA_API_KEY is set. Skips otherwise.

The call is a single short note through GraphRAG-SDK ``Ontology.from_sources`` on the
``LLM_GRAPH`` role (NVIDIA Nemotron). Failures are scrubbed so a key cannot land in the output.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")

from graphrag_sdk import Ontology

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig
from nwb.kg.models import GraphModels
from nwb.kg.ontology import discover_from_sources
from nwb.kg.recording import CallRecorder, RecordMode
from nwb.llm.factory import LLMFactory
from nwb.llm.settings import LLMSettings
from nwb.obs.log import scrub_str

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
_MODEL = "nvidia:nvidia/nemotron-3-super-120b-a12b"


def test_live_discovery_on_nvidia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("NVIDIA_API_KEY"):
        pytest.skip("NVIDIA_API_KEY is not set")
    monkeypatch.setenv("LLM_GRAPH", _MODEL)
    for name in ("LiteLLM", "litellm", "httpx", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    note = tmp_path / "note.md"
    note.write_text("Ava Shah works with the Platform team on the billing service.\n", encoding="utf-8")
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    llm = GraphModels(LLMFactory(LLMSettings()), cfg, tmp_path).sdk_llm(
        CallRecorder(tmp_path / "calls", RecordMode.LIVE)
    )
    try:
        draft = asyncio.run(
            discover_from_sources(
                [str(note)],
                llm,
                method="llm",
                boundaries="a short organisational note",
                sample_chunks_per_doc=1,
                concurrency=1,
                seed=0,
                existing=Ontology(),
            )
        )
    except Exception as exc:
        raise AssertionError(scrub_str(f"{type(exc).__name__}: {exc}")[:500]) from None
    assert draft.entities
    assert all(entity.label for entity in draft.entities)
