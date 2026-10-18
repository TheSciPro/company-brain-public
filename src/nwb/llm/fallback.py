"""Fallback chain for one role.

LangChain's ``with_fallbacks`` chooses the next model. deepagents only accepts a
``BaseChatModel``, so ``FallbackChatModel`` is a thin face over that runnable.
It is not a retry loop.

A hop runs only for 429, HTTP 5xx, timeouts and connection failures. Auth and
validation errors (other 4xx) propagate from the model that raised them.
``ChatNVIDIA`` raises a bare ``Exception`` whose text starts with ``[status]``;
those are re-raised as ``RetryableLLMError`` so ``with_fallbacks`` can see them.

Streaming falls back only when that error happens before the first chunk.
``bind_tools`` keeps the underlying chat models and applies tools at call time,
so a second bind and LangSmith params still see a ``BaseChatModel``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from typing import Any, NoReturn, override

import httpx
import openai
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.language_models.base import LangSmithParams
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import ConfigDict

from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)

_STATUS = re.compile(r"\[(\d{3})\]")
_REQUESTS_TRANSPORT = frozenset({"Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"})


class RetryableLLMError(Exception):
    """A 429, 5xx, timeout or connection failure from a client that does not raise ``openai`` errors."""


RETRYABLE_TYPES: tuple[type[BaseException], ...] = (
    RetryableLLMError,
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    httpx.TimeoutException,
    httpx.ConnectError,
    TimeoutError,
    asyncio.TimeoutError,
)


def http_status(exc: BaseException) -> int | None:
    """Status from an OpenAI/HTTP error, or from ``ChatNVIDIA``'s ``[429] ...`` text."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(response, "status", None)
    if isinstance(code, int):
        return code
    match = _STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def reason(exc: BaseException) -> str:
    """Exception type plus HTTP status. No message body (it can echo the prompt)."""
    code = http_status(exc)
    name = type(exc).__name__
    return f"{name}:{code}" if code is not None else name


def _requests_transport(exc: BaseException) -> bool:
    return type(exc).__module__.startswith("requests") and type(exc).__name__ in _REQUESTS_TRANSPORT


def is_retryable(exc: BaseException) -> bool:
    """True for 429, 5xx, timeouts and connection failures. False for other 4xx."""
    if isinstance(exc, RETRYABLE_TYPES) or _requests_transport(exc):
        return True
    code = http_status(exc)
    return code == 429 or (code is not None and code >= 500)


def is_rate_limit(exc: BaseException) -> bool:
    """True for HTTP 429, including ChatNVIDIA ``[429]`` text and ``RetryableLLMError`` (``Exception:429``).

    ``reason`` drops the response body, so the wrapped form has no brackets and no "rate limit" words.
    A 5xx wrapped the same way is retryable for fallback, but it is not a rate limit.
    """
    if http_status(exc) == 429:
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "ratelimit" in name or "rate limit" in text[:300]:
        return True
    if isinstance(exc, RetryableLLMError) and text.rsplit(":", 1)[-1].strip() == "429":
        return True
    cause = exc.__cause__
    return cause is not None and cause is not exc and is_rate_limit(cause)


async def with_rate_limit_backoff[T](call: Callable[[], Awaitable[T]], waits: tuple[float, ...]) -> T:
    """Wait out a 429 and try again. Other errors propagate on the first attempt."""
    for wait in waits:
        try:
            return await call()
        except Exception as exc:
            if not is_rate_limit(exc):
                raise
            log.info("rate limited; retrying in %.0fs", wait)
            await asyncio.sleep(wait)
    return await call()


def log_fallback(*, provider: str, reason: str, role: str, model: str) -> None:
    """One hop: the provider that failed, why, and which role asked."""
    log.warning("llm fallback", provider=provider, reason=reason, role=role, model=model)
    dataflow(Hop.LLM_FALLBACK, provider=provider, reason=reason, role=role, model=model)


class _Guarded(Runnable[Any, Any]):
    """Re-raise retryable bare exceptions as ``RetryableLLMError`` and log the hop."""

    def __init__(self, inner: Runnable[Any, Any], *, provider: str, model: str, role: str, will_fallback: bool) -> None:
        self.inner = inner
        self.provider = provider
        self.model = model
        self.role = role
        self.will_fallback = will_fallback

    def _reraise(self, exc: Exception) -> NoReturn:
        if not is_retryable(exc):
            raise exc
        if self.will_fallback:
            log_fallback(provider=self.provider, reason=reason(exc), role=self.role, model=self.model)
        if isinstance(exc, RETRYABLE_TYPES):
            raise exc
        raise RetryableLLMError(reason(exc)) from exc

    @override
    def invoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        try:
            return self.inner.invoke(input, config, **kwargs)
        except Exception as exc:
            self._reraise(exc)

    @override
    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        try:
            return await self.inner.ainvoke(input, config, **kwargs)
        except Exception as exc:
            self._reraise(exc)

    def _chunk_source(self, input: Any, kwargs: dict[str, Any], *, async_api: bool) -> Any:
        """Chunks from the inner chat model, without a second traced ``stream`` run.

        LangGraph's messages mode listens to every chat model in the stack. Calling
        the public ``stream`` here would emit each token twice through the chat runner.
        """
        options = dict(kwargs)
        stop = options.pop("stop", None)
        inner = self.inner
        bound = getattr(inner, "bound", None)
        target = bound if isinstance(bound, BaseChatModel) else inner
        if isinstance(target, BaseChatModel):
            extra = dict(getattr(inner, "kwargs", {}) or {})
            if async_api:
                return target._astream(input, stop=stop, **extra, **options)
            return target._stream(input, stop=stop, **extra, **options)
        if async_api:
            return inner.astream(input, **kwargs)
        return inner.stream(input, **kwargs)

    @override
    def stream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> Iterator[Any]:
        """Fall back only if the inner stream fails before its first chunk."""
        del config
        iterator = iter(self._chunk_source(input, kwargs, async_api=False))
        try:
            first = next(iterator)
        except StopIteration:
            return
        except Exception as exc:
            self._reraise(exc)
        yield first
        yield from iterator

    @override
    async def astream(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> AsyncIterator[Any]:
        del config
        iterator = self._chunk_source(input, kwargs, async_api=True)
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            return
        except Exception as exc:
            self._reraise(exc)
        yield first
        async for chunk in iterator:
            yield chunk


def _result(message: Any) -> ChatResult:
    if not isinstance(message, AIMessage):
        message = AIMessage(content=str(getattr(message, "content", message)))
    return ChatResult(generations=[ChatGeneration(message=message)])


def _generation(piece: Any) -> ChatGenerationChunk:
    """``stream`` yields message chunks; ``_stream`` must yield generation chunks."""
    if isinstance(piece, ChatGenerationChunk):
        return piece
    message = piece.message if isinstance(piece, ChatGeneration) else piece
    if isinstance(message, AIMessageChunk):
        return ChatGenerationChunk(message=message)
    text = getattr(message, "content", message)
    return ChatGenerationChunk(message=AIMessageChunk(content=text if isinstance(text, str) else str(text)))


class FallbackChatModel(BaseChatModel):
    """``primary.with_fallbacks(rest)`` behind the ``BaseChatModel`` interface deepagents requires."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(
        self,
        steps: Sequence[BaseChatModel],
        providers: Sequence[str],
        model_ids: Sequence[str],
        role: str,
        *,
        tools: Sequence[Any] | None = None,
        tool_kwargs: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._steps = list(steps)
        self._providers = list(providers)
        self._model_ids = list(model_ids)
        self._role = role
        # Tools stay here. ``bind_tools`` on ChatOpenAI/ChatNVIDIA returns a binding, not a chat model.
        self._tools = None if tools is None else list(tools)
        self._tool_kwargs = dict(tool_kwargs or {})

    @property
    def chain_steps(self) -> list[BaseChatModel]:
        """Underlying chat models. Tool bindings are applied per call and are not stored here."""
        return list(self._steps)

    def _bound_steps(self) -> list[Runnable[Any, Any]]:
        if self._tools is None:
            return list(self._steps)
        return [step.bind_tools(self._tools, **self._tool_kwargs) for step in self._steps]

    @property
    @override
    def _llm_type(self) -> str:
        return "nwb-fallback"

    @override
    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        return self._steps[0]._get_ls_params(stop=stop, **kwargs)

    def _runnable(self) -> Runnable[Any, Any]:
        steps = self._bound_steps()
        last = len(steps) - 1
        guarded = [
            _Guarded(
                step,
                provider=provider,
                model=model_id,
                role=self._role,
                will_fallback=index < last,
            )
            for index, (step, provider, model_id) in enumerate(
                zip(steps, self._providers, self._model_ids, strict=True)
            )
        ]
        if len(guarded) == 1:
            return guarded[0]
        return guarded[0].with_fallbacks(guarded[1:], exceptions_to_handle=RETRYABLE_TYPES)

    @override
    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> FallbackChatModel:
        """Return another fallback model over the same chat models, with these tools."""
        return FallbackChatModel(
            self._steps,
            self._providers,
            self._model_ids,
            self._role,
            tools=tools,
            tool_kwargs=kwargs,
        )

    @override
    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable[LanguageModelInput, Any]:
        bound = [step.with_structured_output(schema, **kwargs) for step in self._steps]
        last = len(bound) - 1
        guarded: list[Runnable[Any, Any]] = [
            _Guarded(step, provider=provider, model=model_id, role=self._role, will_fallback=index < last)
            for index, (step, provider, model_id) in enumerate(
                zip(bound, self._providers, self._model_ids, strict=True)
            )
        ]
        if len(guarded) == 1:
            return guarded[0]
        return guarded[0].with_fallbacks(guarded[1:], exceptions_to_handle=RETRYABLE_TYPES)

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        return _result(self._runnable().invoke(messages, stop=stop, **kwargs))

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        return _result(await self._runnable().ainvoke(messages, stop=stop, **kwargs))

    @override
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del run_manager
        for piece in self._runnable().stream(messages, stop=stop, **kwargs):
            yield _generation(piece)

    @override
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del run_manager
        async for piece in self._runnable().astream(messages, stop=stop, **kwargs):
            yield _generation(piece)


def model_steps(model: BaseChatModel) -> list[BaseChatModel]:
    """The chat models inside a fallback chain, or ``[model]`` when there is no chain."""
    steps = getattr(model, "chain_steps", None)
    if isinstance(steps, list) and steps and all(isinstance(step, BaseChatModel) for step in steps):
        return list(steps)
    return [model]
