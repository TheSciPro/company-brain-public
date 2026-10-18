"""One live yes/no. The answer is recorded and then ignored: different emails still stay apart."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("litellm", reason="litellm is not installed")
pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")

from graphrag_sdk import LiteLLM

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import ResolutionConfig
from nwb.kg.identity import Record, Verdict, decide
from nwb.kg.resolution import confirm_same
from nwb.llm.providers import PROVIDERS

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
_MODEL = "nvidia/nemotron-3-super-120b-a12b"


def test_live_confirm_does_not_merge_people(monkeypatch: pytest.MonkeyPatch) -> None:
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        pytest.skip("NVIDIA_API_KEY is not set")
    monkeypatch.setenv("LITELLM_LOG", "ERROR")
    spec = PROVIDERS["nvidia"]
    llm = LiteLLM(
        model=f"{spec.litellm}/{_MODEL}",
        api_key=key,
        api_base=spec.base_url,
        max_tokens=256,
    )
    answer = asyncio.run(confirm_same(llm, "Acme Company", "Acme", "Organization"))
    assert answer in {"yes", "no"}
    cfg = ConfigLoader(ROOT / "config").load("resolution", ResolutionConfig)
    left = Record("p-1", "Person", "Ada Lovelace", {"email": "ada@example.com", "id": "p-1"})
    right = Record("p-2", "Person", "Ada Lovelace", {"email": "ada.other@example.com", "id": "p-2"})
    verdict, _score = decide(
        left,
        right,
        cfg,
        reason="the same name once legal suffixes and abbreviations are normalised",
        candidates=1,
    )
    assert verdict is Verdict.SEPARATE
