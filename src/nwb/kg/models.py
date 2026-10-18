"""Build GraphRAG-SDK and Graphiti clients from our env-driven model registry, with call recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graphiti_core.llm_client.client import LLMClient as GraphitiLLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphrag_sdk import LiteLLM, LLMInterface

from nwb.conf.models import GraphConfig
from nwb.errors import ConfigError
from nwb.kg.graphiti_llm import FallbackGraphitiClient
from nwb.kg.recording import CallRecorder, RecordedGraphitiLLM, RecordedSdkLLM, RecordMode
from nwb.llm.factory import LLMFactory
from nwb.llm.litellm_log import install_litellm_fallback_log
from nwb.llm.providers import ProviderSpec
from nwb.llm.settings import ModelRef, Role
from nwb.obs.log import get_logger

log = get_logger(__name__)


@dataclass
class GraphModels:
    llm: LLMFactory
    cfg: GraphConfig
    root: Path = Path()
    _noted: set[str] = field(default_factory=set, repr=False)

    def recorder(self, mode: RecordMode | None = None) -> CallRecorder:
        return CallRecorder(self.root / self.cfg.recording.directory, mode or RecordMode(self.cfg.recording.mode))

    def ref(self, role: Role = Role.GRAPH) -> ModelRef:
        return self.llm.primary(role)

    def _refs(self, role: Role, recorder: CallRecorder) -> list[ModelRef]:
        # Replay never calls out, so a missing key must not drop the configured model.
        if recorder.mode is RecordMode.REPLAY:
            return self.llm.configured(role)
        return self.llm.chain(role)

    def _endpoint(self, ref: ModelRef, recorder: CallRecorder) -> tuple[ProviderSpec, str, str]:
        if recorder.mode is RecordMode.REPLAY:  # never calls out, so it needs no key
            spec, base_url = self.llm.provider(ref)
            return spec, base_url, "replay"
        return self.llm.endpoint(ref)

    def _reasoning(self, refs: list[ModelRef]) -> dict[str, Any]:
        effort = self.llm.settings.llm_reasoning_effort
        if effort and all(self.llm.provider(ref)[0].supports_reasoning_effort for ref in refs):
            return {"reasoning_effort": effort}
        return {}

    def sdk_llm(self, recorder: CallRecorder, role: Role = Role.GRAPH) -> LLMInterface:
        """LiteLLM for the role chain.

        When every provider has a LiteLLM prefix (``openrouter/``, ``nvidia_nim/``, ``groq/``),
        the rest of the chain is passed as ``fallbacks`` and LiteLLM switches provider on its
        own retryable errors. Keys are not passed on that path: each prefix reads its env var
        (``nvidia_nim/`` reads ``NVIDIA_NIM_API_KEY``, copied from ``NVIDIA_API_KEY`` at startup
        when unset). A provider with no prefix cannot take part; the first model is used alone.
        """
        refs = self._refs(role, recorder)
        if not refs:
            raise ConfigError(f"no usable LLM for role {role.value}: every provider is missing its API key")
        s = self.llm.settings
        extra: dict[str, Any] = {}
        plan = self.llm.litellm_plan(refs)
        if plan is not None and plan.fallbacks:
            # Every fallback gets the same kwargs, so one provider that rejects the effort drops it for all.
            extra.update(self._reasoning(refs))
            install_litellm_fallback_log()
            extra["fallbacks"] = list(plan.fallbacks)
            extra["metadata"] = {"nwb_role": str(role)}
            # No api_key: one key would be sent to every provider. Env vars are per prefix.
            inner = LiteLLM(model=plan.model, max_tokens=s.llm_max_tokens, **extra)
            return RecordedSdkLLM(inner, recorder)
        if plan is None and len(refs) > 1 and str(role) not in self._noted:
            self._noted.add(str(role))
            log.warning(
                "GraphRAG-SDK cannot fall back for role %s: a provider has no LiteLLM prefix; using %s only",
                role,
                refs[0],
            )
        ref = refs[0]
        spec, base_url, key = self._endpoint(ref, recorder)
        model = f"{spec.litellm}/{ref.model}" if spec.litellm else f"openai/{ref.model}"
        extra.update(self._reasoning([ref]))
        if spec.litellm:
            inner = LiteLLM(model=model, api_key=key, max_tokens=s.llm_max_tokens, **extra)
        else:
            inner = LiteLLM(model=model, api_key=key, api_base=base_url, max_tokens=s.llm_max_tokens, **extra)
        return RecordedSdkLLM(inner, recorder)

    def graphiti_llm(self, recorder: CallRecorder, role: Role = Role.GRAPH) -> GraphitiLLMClient:
        """Graphiti client for the role chain.

        Live and record modes use ``FallbackGraphitiClient``: Graphiti accepts any
        ``LLMClient``, and that subclass sends each call through the same chat model
        as the agent (``with_fallbacks`` on 429, 5xx, and timeouts). Replay never calls
        out, so it keeps an OpenAI-compatible client with a placeholder key and does
        not require provider keys to be set.
        """
        refs = self._refs(role, recorder)
        if not refs:
            raise ConfigError(f"no usable LLM for role {role.value}: every provider is missing its API key")
        ref = refs[0]
        cfg = LLMConfig(
            model=ref.model,
            small_model=ref.model,
            max_tokens=self.llm.settings.llm_max_tokens,
            temperature=0.0,
        )
        if recorder.mode is RecordMode.REPLAY:
            _spec, base_url, key = self._endpoint(ref, recorder)
            cfg.base_url = base_url
            cfg.api_key = key
            inner: GraphitiLLMClient = OpenAIGenericClient(cfg)
        else:
            inner = FallbackGraphitiClient(self.llm.for_role(role), cfg)
        return RecordedGraphitiLLM(inner, recorder)
