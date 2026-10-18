"""bind_tools stays on chat models; streaming falls back only before the first chunk."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import openai
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables.base import RunnableBinding

from nwb.llm.fallback import (
    FallbackChatModel,
    RetryableLLMError,
    is_rate_limit,
    model_steps,
    reason,
    with_rate_limit_backoff,
)


def _rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)


def _chain(primary: BaseChatModel, fallback: BaseChatModel) -> FallbackChatModel:
    return FallbackChatModel([primary, fallback], ["nvidia", "openrouter"], ["a", "b"], "main")


class _BindingModel(FakeListChatModel):
    """``bind_tools`` returns a ``RunnableBinding``, the way ChatOpenAI and ChatNVIDIA do."""

    def __init__(self, seen: list[Any]) -> None:
        super().__init__(responses=["ok"])
        object.__setattr__(self, "seen", seen)

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> Any:  # type: ignore[override]
        return self.bind(tools=list(tools), **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager
        self.seen.append(kwargs.get("tools"))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


class _ChunkModel(FakeListChatModel):
    def __init__(
        self,
        parts: list[str],
        calls: list[int],
        *,
        fail_at: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(responses=[""])
        object.__setattr__(self, "parts", parts)
        object.__setattr__(self, "calls", calls)
        object.__setattr__(self, "fail_at", fail_at)
        object.__setattr__(self, "error", error)

    def _chunks(self) -> Iterator[ChatGenerationChunk]:
        self.calls[0] += 1
        for index, part in enumerate(self.parts):
            if self.error is not None and self.fail_at == index:
                raise self.error
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        yield from self._chunks()

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        for chunk in self._chunks():
            yield chunk


def test_second_bind_tools_keeps_chat_models_and_replaces_tools() -> None:
    seen: list[Any] = []
    primary, fallback = _BindingModel(seen), _BindingModel([])
    chain = _chain(primary, fallback)
    first = [{"name": "one"}]
    second = [{"name": "two"}]
    once = chain.bind_tools(first)
    twice = once.bind_tools(second)

    assert all(isinstance(step, BaseChatModel) for step in twice.chain_steps)
    assert not any(isinstance(step, RunnableBinding) for step in twice.chain_steps)
    assert model_steps(twice) == [primary, fallback]
    assert twice._get_ls_params() == primary._get_ls_params()
    assert twice.invoke("hi").content == "ok"
    assert seen == [second]


def test_stream_falls_back_only_before_the_first_chunk() -> None:
    primary_calls, fallback_calls = [0], [0]
    primary = _ChunkModel(["no"], primary_calls, fail_at=0, error=_rate_limit())
    fallback = _ChunkModel(["ye", "s"], fallback_calls)
    chunks = list(_chain(primary, fallback).stream("hi"))
    assert "".join(str(chunk.content) for chunk in chunks) == "yes"
    assert len(chunks) >= 2
    assert (primary_calls[0], fallback_calls[0]) == (1, 1)


def test_stream_error_after_first_chunk_does_not_fall_back() -> None:
    primary_calls, fallback_calls = [0], [0]
    primary = _ChunkModel(["a", "b"], primary_calls, fail_at=1, error=_rate_limit())
    fallback = _ChunkModel(["no"], fallback_calls)
    got: list[str] = []

    def consume() -> None:
        for chunk in _chain(primary, fallback).stream("hi"):
            got.append(str(chunk.content))

    with pytest.raises(openai.RateLimitError):
        consume()
    assert "a" in got
    assert fallback_calls[0] == 0


def test_astream_falls_back_before_the_first_chunk() -> None:
    primary_calls, fallback_calls = [0], [0]
    primary = _ChunkModel(["no"], primary_calls, fail_at=0, error=Exception("[429] busy"))
    fallback = _ChunkModel(["ok"], fallback_calls)

    async def collect() -> str:
        parts = [str(chunk.content) async for chunk in _chain(primary, fallback).astream("hi")]
        return "".join(parts)

    assert asyncio.run(collect()) == "ok"
    assert fallback_calls[0] == 1


def test_backoff_retries_both_nvidia_429_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("nwb.llm.fallback.asyncio.sleep", no_sleep)

    async def retries(exc: BaseException) -> None:
        calls = {"n": 0}

        async def call() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise exc
            return "ok"

        assert await with_rate_limit_backoff(call, (0.0,)) == "ok"
        assert calls["n"] == 2

    asyncio.run(retries(Exception("[429] Too Many Requests")))
    asyncio.run(retries(RetryableLLMError("Exception:429")))


def test_backoff_does_not_retry_other_client_errors() -> None:
    async def call() -> str:
        raise Exception("[401] unauthorized")

    with pytest.raises(Exception, match="401"):
        asyncio.run(with_rate_limit_backoff(call, (0.0, 0.0)))


def test_rate_limit_sees_nvidia_text_and_retryable_wrapper() -> None:
    """ChatNVIDIA says ``[429] ...``; the chain re-raises that as ``RetryableLLMError`` (``Exception:429``)."""
    bare = Exception("[429] Too Many Requests")
    wrapped = RetryableLLMError(reason(bare))
    assert str(wrapped) == "Exception:429"
    assert is_rate_limit(bare)
    assert is_rate_limit(wrapped)
    assert is_rate_limit(RetryableLLMError("Exception:429"))
    assert is_rate_limit(_rate_limit())
    assert not is_rate_limit(Exception("[503] unavailable"))
    assert not is_rate_limit(RetryableLLMError("Exception:503"))
    assert not is_rate_limit(Exception("[401] unauthorized"))


def test_stream_client_error_before_first_chunk_does_not_fall_back() -> None:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    denied = openai.AuthenticationError("no", response=httpx.Response(401, request=request), body=None)
    primary_calls, fallback_calls = [0], [0]
    primary = _ChunkModel(["no"], primary_calls, fail_at=0, error=denied)
    fallback = _ChunkModel(["ok"], fallback_calls)
    with pytest.raises(openai.AuthenticationError):
        list(_chain(primary, fallback).stream("hi"))
    assert fallback_calls[0] == 0
