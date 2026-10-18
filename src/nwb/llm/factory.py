"""Build one chat model per role: an ordered provider chain from env, via ``with_fallbacks``."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult

from nwb.errors import ConfigError
from nwb.llm.fallback import FallbackChatModel
from nwb.llm.providers import PROVIDERS, ProviderSpec
from nwb.llm.settings import LLMSettings, ModelRef, Role
from nwb.obs.log import Hop, dataflow, get_logger

log = get_logger(__name__)
ChatBuilder = Callable[..., BaseChatModel]

# LiteLLM reads these names. NVIDIA's prefix is ``nvidia_nim/`` and looks up NVIDIA_NIM_API_KEY,
# not NVIDIA_API_KEY. The others match the settings fields; set only when the process env is empty.
_LITELLM_ENV: dict[str, tuple[str, ...]] = {
    "nvidia_api_key": ("NVIDIA_NIM_API_KEY",),
    "openrouter_api_key": ("OPENROUTER_API_KEY",),
    "groq_api_key": ("GROQ_API_KEY",),
    "google_api_key": ("GOOGLE_API_KEY",),
}


@dataclass(frozen=True, slots=True)
class LitellmPlan:
    """One LiteLLM model id plus fallbacks, each ``prefix/model`` (for example ``nvidia_nim/...``)."""

    model: str
    fallbacks: tuple[str, ...]


def publish_litellm_keys(settings: LLMSettings) -> None:
    """Copy configured keys into the env vars LiteLLM reads, when those vars are unset."""
    for field, names in _LITELLM_ENV.items():
        secret = getattr(settings, field)
        if secret is None:
            continue
        value = str(secret.get_secret_value())
        for name in names:
            if not os.environ.get(name):
                os.environ[name] = value


def _init_model(**kw: Any) -> BaseChatModel:
    """``init_chat_model``: NVIDIA goes to ``ChatNVIDIA``; everyone else is OpenAI-compatible chat completions."""
    provider = kw.pop("model_provider")
    model = kw.pop("model")
    if provider == "nvidia":
        # ChatNVIDIA would forward these as model_kwargs and the API would 400.
        kw.pop("max_retries", None)
        kw.pop("reasoning_effort", None)
        max_tokens = kw.pop("max_tokens", None)
        if max_tokens is not None:
            kw["max_completion_tokens"] = max_tokens
        kw.setdefault("usage_telemetry_enabled", False)
        built = init_chat_model(model, model_provider="nvidia", **kw)
    else:
        kw["use_responses_api"] = False
        if not kw.get("reasoning_effort"):
            kw.pop("reasoning_effort", None)
        built = init_chat_model(model, model_provider="openai", **kw)
    if not isinstance(built, BaseChatModel):
        raise ConfigError(f"init_chat_model returned {type(built).__name__}, expected a chat model")
    return built


class DataflowCallback(BaseCallbackHandler):
    """Records every LLM call (model + token usage) as a data-flow hop. No prompt bodies."""

    def __init__(self, ref: ModelRef, role: Role) -> None:
        self._ref, self._role = ref, role

    def on_llm_end(self, response: LLMResult, **_: Any) -> None:
        usage = (response.llm_output or {}).get("token_usage") or {}
        dataflow(
            Hop.LLM_CALL,
            role=str(self._role),
            model=str(self._ref),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


class LLMFactory:
    def __init__(
        self,
        settings: LLMSettings,
        providers: Mapping[str, ProviderSpec] = PROVIDERS,
        builder: ChatBuilder = _init_model,
    ) -> None:
        self._s = settings
        self._providers = providers
        self._build = builder
        warned: set[str] = set()
        self._chains = {role: self._usable(self._s.models_for(role), warned) for role in Role}
        publish_litellm_keys(settings)

    @property
    def settings(self) -> LLMSettings:
        return self._s

    def configured(self, role: Role) -> list[ModelRef]:
        """The chain as written, including providers with no key (replay does not call them)."""
        return list(self._s.models_for(role))

    def chain(self, role: Role) -> list[ModelRef]:
        """Providers for ``role`` whose key is set, in order. Resolved once, at startup."""
        return list(self._chains[role])

    def primary(self, role: Role) -> ModelRef:
        refs = self.chain(role)
        if not refs:
            raise ConfigError(f"no usable LLM for role {role.value}: every provider is missing its API key")
        return refs[0]

    def litellm_plan(self, refs: Sequence[ModelRef]) -> LitellmPlan | None:
        """LiteLLM ids for ``refs``. ``None`` when any provider has no LiteLLM prefix (no cross-provider fallback)."""
        if not refs:
            return None
        ids: list[str] = []
        for ref in refs:
            spec = self._providers.get(ref.provider)
            if spec is None or not spec.litellm:
                return None
            ids.append(f"{spec.litellm}/{ref.model}")
        return LitellmPlan(ids[0], tuple(ids[1:]))

    def provider(self, ref: ModelRef) -> tuple[ProviderSpec, str]:
        """(provider spec, base_url) for ``ref``; no key involved."""
        spec = self._providers.get(ref.provider)
        if spec is None:
            raise ConfigError(f"unknown LLM provider {ref.provider!r}; known: {sorted(self._providers)}")
        base_url = self._s.ollama_base_url if spec.name == "ollama" and self._s.ollama_base_url else spec.base_url
        return spec, base_url

    def endpoint(self, ref: ModelRef) -> tuple[ProviderSpec, str, str]:
        """(provider spec, base_url, api key) for ``ref``. The key is returned for client construction only."""
        spec, base_url = self.provider(ref)
        return spec, base_url, self._key(spec)

    def chat(self, ref: ModelRef, role: Role = Role.MAIN) -> BaseChatModel:
        spec, base_url, key = self.endpoint(ref)
        kw: dict[str, Any] = {
            "model": ref.model,
            "model_provider": spec.chat_provider,
            "base_url": base_url,
            "api_key": key,
            "temperature": self._s.llm_temperature,
            "timeout": self._s.llm_timeout_s,
            "max_retries": self._s.llm_max_retries,
            "max_tokens": self._s.llm_max_tokens,
            "callbacks": [DataflowCallback(ref, role)],
        }
        if self._s.llm_reasoning_effort:
            kw["reasoning_effort"] = self._s.llm_reasoning_effort
        return self._build(**kw)

    def for_role(self, role: Role) -> BaseChatModel:
        """First usable model, with the rest attached by ``with_fallbacks`` (429 / 5xx / timeout only)."""
        refs = self.chain(role)
        if not refs:
            raise ConfigError(f"no usable LLM for role {role.value}: every provider is missing its API key")
        models = [self.chat(ref, role) for ref in refs]
        if len(models) == 1:
            return models[0]
        return FallbackChatModel(models, [ref.provider for ref in refs], [ref.model for ref in refs], str(role))

    def _usable(self, refs: Sequence[ModelRef], warned: set[str]) -> list[ModelRef]:
        ok: list[ModelRef] = []
        for ref in refs:
            spec = self._providers.get(ref.provider)
            if spec is None:
                ok.append(ref)  # chat() raises ConfigError; a typo is not a missing key
                continue
            if spec.key_field and getattr(self._s, spec.key_field) is None:
                if spec.name not in warned:
                    warned.add(spec.name)
                    log.warning("skipping LLM provider %s: %s is not set", spec.name, spec.key_field.upper())
                continue
            ok.append(ref)
        return ok

    def _key(self, spec: ProviderSpec) -> str:
        if spec.key_field is None:
            return "not-needed"
        secret = getattr(self._s, spec.key_field)
        if secret is None:
            raise ConfigError(f"{spec.key_field.upper()} is not set (needed for provider {spec.name!r})")
        return str(secret.get_secret_value())
