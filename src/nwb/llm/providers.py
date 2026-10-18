"""Registry of chat providers. Adding a provider = adding one entry here."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    base_url: str
    key_field: str | None  # attribute on LLMSettings holding the key; None = no key (local)
    litellm: str = ""  # LiteLLM provider prefix (GraphRAG-SDK); empty = OpenAI-compatible via base_url
    chat_provider: str = "openai"  # init_chat_model model_provider; "nvidia" -> ChatNVIDIA
    supports_reasoning_effort: bool = True  # LiteLLM's nvidia_nim adapter rejects this parameter


PROVIDERS: MappingProxyType[str, ProviderSpec] = MappingProxyType(
    {
        p.name: p
        for p in (
            ProviderSpec("groq", "https://api.groq.com/openai/v1", "groq_api_key", "groq"),
            ProviderSpec(
                "nvidia",
                "https://integrate.api.nvidia.com/v1",
                "nvidia_api_key",
                "nvidia_nim",
                chat_provider="nvidia",
                supports_reasoning_effort=False,
            ),
            ProviderSpec("openrouter", "https://openrouter.ai/api/v1", "openrouter_api_key", "openrouter"),
            ProviderSpec("google", "https://generativelanguage.googleapis.com/v1beta/openai/", "google_api_key"),
            ProviderSpec("ollama", "http://localhost:11434/v1", None),
        )
    }
)
