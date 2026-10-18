"""Graphiti ``LLMClient`` that calls the role's chat model.

``graphiti_core.llm_client.LLMClient`` is the extension point. ``Graphiti(...)``
stores whatever ``LLMClient`` it is given, and ``generate_response`` on the base
class injects the JSON schema, then calls ``_generate_response``. A subclass
only implements that method. The chat model is the same chain the agent uses,
so a 429, 5xx, or timeout hops to the next provider before Graphiti sees it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.llm_client.errors import EmptyResponseError
from graphiti_core.prompts.models import Message
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n?")
_FENCE_CLOSE = re.compile(r"\r?\n?```[ \t]*$")


def message_text(content: Any) -> str:
    """Flatten a chat-model content value to text."""
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


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, stripping one markdown fence. Empty input is ``EmptyResponseError``."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", stripped)).strip()
    if not stripped:
        raise EmptyResponseError("LLM returned an empty response")
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("expected a JSON object", stripped, 0)
    return parsed


def to_langchain(messages: list[Message]) -> list[BaseMessage]:
    """Graphiti role/content messages as LangChain messages."""
    out: list[BaseMessage] = []
    for message in messages:
        if message.role == "system":
            out.append(SystemMessage(content=message.content))
        elif message.role == "assistant":
            out.append(AIMessage(content=message.content))
        else:
            out.append(HumanMessage(content=message.content))
    return out


class FallbackGraphitiClient(LLMClient):
    """One Graphiti client for the whole provider chain."""

    def __init__(self, chat: BaseChatModel, config: LLMConfig | None = None) -> None:
        super().__init__(config, cache=False)
        self._chat = chat

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        # The schema is already on the prompt (base ``generate_response``). ``model`` / ``model_size``
        # would pick a single id and skip the chain, so they are ignored. Temperature comes from
        # ``LLMConfig`` (Graphiti extraction uses 0). Token cap is the one baked into the chat model:
        # passing ``max_tokens`` makes ChatNVIDIA reject the request.
        del response_model, max_tokens, model_size, model
        result = await self._chat.ainvoke(to_langchain(messages), temperature=self.temperature)
        return parse_json_object(message_text(result.content))
