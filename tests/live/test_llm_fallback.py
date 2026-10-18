"""Two live calls against the default chain. Off unless NWB_LIVE=1.

Call 1: the role chain (NVIDIA primary; OpenRouter only if that hop is 429/5xx/timeout).
Call 2: ChatNVIDIA directly. Together with a possible fallback hop this stays under six calls.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from nwb.llm import LLMFactory, LLMSettings, Role
from nwb.llm.fallback import FallbackChatModel
from nwb.llm.settings import DEFAULT_CHAIN

pytestmark = pytest.mark.live

_PROMPT = "Reply with the single word pong."


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts).strip()
    return str(content).strip()


def _settings(monkeypatch: pytest.MonkeyPatch) -> LLMSettings:
    for key in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY"):
        if not os.environ.get(key):
            pytest.skip(f"{key} is not set")
    chain = ",".join(str(ref) for ref in DEFAULT_CHAIN)
    monkeypatch.setenv("LLM_MAIN", chain)
    monkeypatch.setenv("LLM_BACKGROUND", chain)
    monkeypatch.setenv("LLM_GRAPH", chain)
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    monkeypatch.setenv("LLM_MAX_TOKENS", "64")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    return LLMSettings(_env_file=None)  # type: ignore[call-arg]


def _invoke(model: Any) -> str:
    try:
        message = model.invoke(_PROMPT)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        raise AssertionError(f"live call failed: {type(exc).__name__}:{status}") from None
    text = _text(message.content)
    assert text, "live call returned empty content"
    return text


def test_default_chain_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = LLMFactory(_settings(monkeypatch))
    assert [ref.provider for ref in factory.chain(Role.MAIN)] == ["nvidia", "openrouter"]
    chain = factory.for_role(Role.MAIN)
    assert isinstance(chain, FallbackChatModel)
    assert _invoke(chain)
    nvidia = factory.chain(Role.MAIN)[0]
    direct = factory.chat(nvidia, Role.MAIN)
    assert type(direct).__name__ == "ChatNVIDIA"
    assert _invoke(direct)
