"""Graphiti's LLMClient subclass delegates to the provider chain. No network."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import openai
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from nwb.llm.fallback import FallbackChatModel

pytest.importorskip("graphiti_core")

from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.errors import EmptyResponseError
from graphiti_core.prompts.models import Message

from nwb.kg.graphiti_llm import FallbackGraphitiClient, parse_json_object


def _http_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return cls("nope", response=httpx.Response(status, request=request), body=None)


def _scripted(text: str, error: BaseException | None = None) -> tuple[FakeListChatModel, dict[str, int]]:
    calls = {"n": 0}

    class _Model(FakeListChatModel):
        def _generate(
            self, messages: list[Any], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
        ) -> Any:
            calls["n"] += 1
            if error is not None:
                raise error
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    return _Model(responses=[text]), calls


def _client(chat: FallbackChatModel) -> FallbackGraphitiClient:
    return FallbackGraphitiClient(chat, LLMConfig(model="primary", small_model="primary", temperature=0.0))


def test_subclass_is_a_graphiti_client() -> None:
    primary, _calls = _scripted('{"ok": true}')
    client = _client(FallbackChatModel([primary], ["nvidia"], ["m"], "graph"))
    assert isinstance(client, LLMClient)


def test_retryable_error_falls_back_to_json() -> None:
    primary, primary_calls = _scripted("no", _http_error(openai.RateLimitError, 429))
    fallback, fallback_calls = _scripted('```json\n{"kept": 1}\n```')
    client = _client(FallbackChatModel([primary, fallback], ["nvidia", "openrouter"], ["a", "b"], "graph"))
    out = asyncio.run(client._generate_response([Message(role="user", content="hi")]))
    assert out == {"kept": 1}
    assert (primary_calls["n"], fallback_calls["n"]) == (1, 1)


def test_auth_error_does_not_fall_back() -> None:
    primary, _primary_calls = _scripted("no", _http_error(openai.AuthenticationError, 401))
    fallback, fallback_calls = _scripted('{"ok": true}')
    client = _client(FallbackChatModel([primary, fallback], ["nvidia", "openrouter"], ["a", "b"], "graph"))
    with pytest.raises(openai.AuthenticationError):
        asyncio.run(client._generate_response([Message(role="user", content="hi")]))
    assert fallback_calls["n"] == 0


def test_empty_response_is_graphitis_error() -> None:
    primary, _calls = _scripted("   ")
    client = _client(FallbackChatModel([primary], ["nvidia"], ["m"], "graph"))
    with pytest.raises(EmptyResponseError):
        asyncio.run(client._generate_response([Message(role="user", content="hi")]))


def test_parse_json_object_rejects_a_list() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_json_object("[1]")
