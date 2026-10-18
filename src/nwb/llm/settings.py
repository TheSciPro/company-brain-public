"""LLM settings from env. A model is ``provider:model``; a role is a comma-separated chain."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Role(StrEnum):
    MAIN = "main"  # the chat agent
    BACKGROUND = "background"  # memory extraction, notes, summaries
    GRAPH = "graph"  # GraphRAG-SDK / Graphiti extraction (defaults to the background chain)


@dataclass(frozen=True, slots=True)
class ModelRef:
    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> ModelRef:
        provider, sep, model = spec.strip().partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"model spec must be 'provider:model', got {spec!r}")
        return cls(provider.lower(), model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


# NVIDIA first (no daily cap), then OpenRouter's free tier. Override with LLM_MAIN / LLM_BACKGROUND / LLM_GRAPH.
DEFAULT_CHAIN: tuple[ModelRef, ...] = (
    ModelRef("nvidia", "nvidia/nemotron-3-super-120b-a12b"),
    ModelRef("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
)


def _parse_chain(text: str) -> tuple[ModelRef, ...]:
    return tuple(ModelRef.parse(part) for part in text.split(",") if part.strip())


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_main: Annotated[tuple[ModelRef, ...], NoDecode] = DEFAULT_CHAIN
    llm_background: Annotated[tuple[ModelRef, ...], NoDecode] = DEFAULT_CHAIN
    llm_graph: Annotated[tuple[ModelRef, ...] | None, NoDecode] = None  # None = use the background chain
    llm_temperature: float = 0.2
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 0  # OpenAI-compatible clients only; the chain is the retry, not a per-host loop
    llm_max_tokens: int = 4096
    llm_reasoning_effort: str = ""  # low|medium|high; empty = don't send (nemotron rejects an unknown effort)

    groq_api_key: SecretStr | None = None
    nvidia_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    ollama_base_url: str = ""  # override for ollama, e.g. http://host:11434/v1

    @field_validator("llm_main", "llm_background", "llm_graph", mode="before")
    @classmethod
    def _chain(cls, v: object) -> object:
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return None
            return _parse_chain(text)
        return v

    def models_for(self, role: Role) -> tuple[ModelRef, ...]:
        """Configured chain for ``role``, including providers whose key is missing."""
        if role is Role.MAIN:
            return self.llm_main
        if role is Role.GRAPH and self.llm_graph is not None:
            return self.llm_graph
        return self.llm_background

    def model_for(self, role: Role) -> ModelRef:
        """First configured model. The usable chain (keys present) is ``LLMFactory.chain``."""
        return self.models_for(role)[0]
