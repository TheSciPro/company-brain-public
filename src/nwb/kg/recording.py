"""Record/replay for LLM calls made inside GraphRAG-SDK and Graphiti.

Extraction is the expensive, rate-limited part (the ``LLM_GRAPH`` chain). Every call is
stored as one JSON file keyed by a hash of (model, call kind, payload). Tests run in ``replay`` mode: a miss is an
error, never a network call, so the suite is hermetic. Scripts run in ``record`` mode and only pay for new calls.
UUIDs are masked in the key because the libraries mint fresh ones per run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from graphiti_core.llm_client.client import LLMClient as GraphitiLLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, ModelSize
from graphiti_core.prompts.models import Message
from graphrag_sdk import LLMInterface
from graphrag_sdk.core.models import ChatMessage, LLMResponse
from pydantic import BaseModel

from nwb.llm.fallback import with_rate_limit_backoff

RATE_LIMIT_WAITS: tuple[float, ...] = (15.0, 30.0, 45.0, 60.0, 60.0)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class RecordMode(StrEnum):
    LIVE = "live"  # always call, never store
    RECORD = "record"  # hit -> stored answer; miss -> call and store
    REPLAY = "replay"  # hit -> stored answer; miss -> RecordingMissError (tests)


class RecordingMissError(LookupError):
    """A replay-mode call had no recording. Re-record with the matching script."""


@dataclass
class CallRecorder:
    directory: Path
    mode: RecordMode = RecordMode.REPLAY
    hits: int = 0
    misses: int = 0

    @staticmethod
    def key(model: str, kind: str, payload: Any) -> str:
        blob = json.dumps({"model": model, "kind": kind, "payload": payload}, sort_keys=True, default=str)
        return hashlib.sha256(_UUID.sub("<uuid>", blob).encode()).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> Any | None:
        if self.mode is RecordMode.LIVE:
            return None
        p = self._path(key)
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text(encoding="utf-8"))["response"]
        self.misses += 1
        if self.mode is RecordMode.REPLAY:
            raise RecordingMissError(f"no recording {key} in {self.directory}")
        return None

    def put(self, key: str, model: str, kind: str, response: Any) -> None:
        if self.mode is RecordMode.LIVE:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        doc = {"model": model, "kind": kind, "response": response}
        self._path(key).write_text(json.dumps(doc, indent=1, sort_keys=True, default=str), encoding="utf-8")


class RecordedSdkLLM(LLMInterface):
    """GraphRAG-SDK ``LLMInterface`` that records the wrapped provider (e.g. ``LiteLLM``)."""

    def __init__(self, inner: LLMInterface, recorder: CallRecorder) -> None:
        super().__init__(
            model_name=inner.model_name, model_params=inner.model_params, max_concurrency=inner.max_concurrency
        )
        self._inner = inner
        self._rec = recorder

    def invoke(self, prompt: str, **kwargs: Any) -> LLMResponse:
        key = self._rec.key(self.model_name, "prompt", {"prompt": prompt, "kw": kwargs})
        hit = self._rec.get(key)
        if hit is not None:
            return LLMResponse(content=hit)
        out = self._inner.invoke(prompt, **kwargs)
        self._rec.put(key, self.model_name, "prompt", out.content)
        return out

    async def ainvoke(
        self, prompt: str, *, max_retries: int = 3, timeout: float | None = None, **kwargs: Any
    ) -> LLMResponse:
        key = self._rec.key(self.model_name, "prompt", {"prompt": prompt, "kw": kwargs})
        hit = self._rec.get(key)
        if hit is not None:
            return LLMResponse(content=hit)
        out = await with_rate_limit_backoff(
            lambda: self._inner.ainvoke(prompt, max_retries=1, timeout=timeout, **kwargs), RATE_LIMIT_WAITS
        )
        self._rec.put(key, self.model_name, "prompt", out.content)
        return out

    async def ainvoke_messages(
        self, messages: list[ChatMessage], *, max_retries: int = 3, timeout: float | None = None, **kwargs: Any
    ) -> LLMResponse:
        payload = {"messages": [m.model_dump(mode="json") for m in messages], "kw": kwargs}
        key = self._rec.key(self.model_name, "messages", payload)
        hit = self._rec.get(key)
        if hit is not None:
            return LLMResponse(content=hit)
        out = await with_rate_limit_backoff(
            lambda: self._inner.ainvoke_messages(messages, max_retries=1, timeout=timeout, **kwargs),
            RATE_LIMIT_WAITS,
        )
        self._rec.put(key, self.model_name, "messages", out.content)
        return out

    async def astream(self, prompt: str, *, timeout: float | None = None, **kwargs: Any) -> AsyncIterator[str]:
        resp = await self.ainvoke(prompt, timeout=timeout, **kwargs)
        yield resp.content


class RecordedGraphitiLLM(GraphitiLLMClient):
    """Graphiti ``LLMClient`` that records the wrapped client's structured responses."""

    def __init__(self, inner: GraphitiLLMClient, recorder: CallRecorder) -> None:
        super().__init__(inner.config, cache=False)
        self._inner = inner
        self._rec = recorder

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        name = model or (self.config.small_model if model_size is ModelSize.small else self.config.model) or ""
        payload = {
            "messages": [m.model_dump(mode="json") for m in messages],
            "schema": response_model.__name__ if response_model else None,
        }
        key = self._rec.key(name, "graphiti", payload)
        hit = self._rec.get(key)
        if hit is not None:
            return dict(hit)
        out = await with_rate_limit_backoff(
            lambda: self._inner._generate_response(messages, response_model, max_tokens, model_size, model=model),
            RATE_LIMIT_WAITS,
        )
        self._rec.put(key, name, "graphiti", out)
        return out
