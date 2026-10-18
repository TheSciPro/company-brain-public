"""LLM config: env chains, with_fallbacks, missing keys. No network except tests/live."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables.fallbacks import RunnableWithFallbacks

from nwb.errors import ConfigError
from nwb.llm import PROVIDERS, LLMFactory, LLMSettings, ModelRef, ProviderSpec, Role
from nwb.llm.factory import LitellmPlan
from nwb.llm.fallback import FallbackChatModel
from nwb.llm.litellm_log import record_litellm_failure
from nwb.llm.settings import DEFAULT_CHAIN

ROOT = Path(__file__).resolve().parents[2]
ENV_KEYS = (
    "LLM_MAIN",
    "LLM_BACKGROUND",
    "LLM_GRAPH",
    "LLM_REASONING_EFFORT",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "NVIDIA_NIM_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "OLLAMA_BASE_URL",
)
CHAIN = ",".join(str(ref) for ref in DEFAULT_CHAIN)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> FakeListChatModel:
        self.calls.append(kw)
        return FakeListChatModel(responses=[kw["model"]])


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> LLMSettings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return LLMSettings(_env_file=None)  # type: ignore[call-arg]


def test_parse_model_ref_splits_on_first_colon() -> None:
    assert ModelRef.parse("openrouter:nvidia/nemotron-3-super-120b-a12b:free") == ModelRef(
        "openrouter", "nvidia/nemotron-3-super-120b-a12b:free"
    )
    with pytest.raises(ValueError, match="provider:model"):
        ModelRef.parse("gpt-oss-120b")


def test_env_example_lists_the_default_chain() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for role in ("LLM_MAIN", "LLM_BACKGROUND", "LLM_GRAPH"):
        assert f"{role}={CHAIN}" in text
    assert "Graphiti uses the same chain" in text
    assert "Graphiti cannot fall back" not in text


def test_default_chain_and_litellm_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = LLMFactory(settings(monkeypatch, OPENROUTER_API_KEY="o", NVIDIA_API_KEY="n"), builder=Recorder())
    assert [ref.provider for ref in factory.chain(Role.MAIN)] == ["nvidia", "openrouter"]
    assert [ref.provider for ref in factory.chain(Role.GRAPH)] == ["nvidia", "openrouter"]
    assert factory.litellm_plan(factory.chain(Role.GRAPH)) == LitellmPlan(
        "nvidia_nim/nvidia/nemotron-3-super-120b-a12b",
        ("openrouter/nvidia/nemotron-3-super-120b-a12b:free",),
    )
    assert os_nim() == "n"


def os_nim() -> str:
    return os.environ["NVIDIA_NIM_API_KEY"]


def test_nim_key_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "already")
    LLMFactory(settings(monkeypatch, NVIDIA_API_KEY="nvapi-test"), builder=Recorder())
    assert os_nim() == "already"


def test_env_drives_each_role(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(
        monkeypatch,
        LLM_MAIN="groq:openai/gpt-oss-120b,nvidia:z-ai/glm-5.3",
        LLM_BACKGROUND="groq:openai/gpt-oss-20b",
        LLM_GRAPH="openrouter:nvidia/nemotron-3-super-120b-a12b:free",
        GROQ_API_KEY="gsk_test",
        NVIDIA_API_KEY="nvapi-test",
        OPENROUTER_API_KEY="or-test",
    )
    assert s.model_for(Role.BACKGROUND).model == "openai/gpt-oss-20b"
    rec = Recorder()
    chain = LLMFactory(s, builder=rec).for_role(Role.MAIN)
    assert isinstance(chain, FallbackChatModel)
    assert [(c["model"], c["base_url"]) for c in rec.calls] == [
        ("openai/gpt-oss-120b", PROVIDERS["groq"].base_url),
        ("z-ai/glm-5.3", PROVIDERS["nvidia"].base_url),
    ]
    assert rec.calls[0]["api_key"] == "gsk_test"


def test_litellm_plan_is_none_without_a_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, LLM_GRAPH="ollama:qwen3,groq:openai/gpt-oss-20b", GROQ_API_KEY="gsk_x")
    factory = LLMFactory(s, builder=Recorder())
    assert factory.litellm_plan(factory.chain(Role.GRAPH)) is None


@pytest.mark.parametrize(
    "error",
    [
        _http_error(openai.RateLimitError, 429),
        _http_error(openai.InternalServerError, 503),
        openai.APITimeoutError(httpx.Request("POST", "https://example.test/v1/chat/completions")),
        Exception("[429] Too Many Requests"),
        TimeoutError("timed out"),
    ],
)
def test_retryable_error_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    s = settings(monkeypatch, LLM_MAIN="openrouter:bad,nvidia:good", OPENROUTER_API_KEY="o", NVIDIA_API_KEY="n")
    primary, primary_calls = _scripted("no", error)
    fallback, fallback_calls = _scripted("ok")
    built = iter((primary, fallback))

    def builder(**kw: Any) -> FakeListChatModel:
        del kw
        return next(built)

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("nwb.dataflow"), "level", logging.INFO)
    model = LLMFactory(s, builder=builder).for_role(Role.MAIN)
    assert isinstance(model._runnable(), RunnableWithFallbacks)  # type: ignore[attr-defined]
    assert model.invoke("hi").content == "ok"
    assert (primary_calls["n"], fallback_calls["n"]) == (1, 1)
    hops = [r for r in caplog.records if r.getMessage() == "llm fallback"]
    assert len(hops) == 1
    assert hops[0].fields["provider"] == "openrouter"  # type: ignore[attr-defined]
    assert hops[0].fields["role"] == "main"  # type: ignore[attr-defined]
    assert hops[0].fields["model"] == "bad"  # type: ignore[attr-defined]
    assert any(r.getMessage() == "llm.fallback" for r in caplog.records)


@pytest.mark.parametrize(
    "error",
    [
        _http_error(openai.AuthenticationError, 401),
        _http_error(openai.BadRequestError, 400),
        Exception("[401] unauthorized"),
        Exception("[422] invalid"),
    ],
)
def test_client_error_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: BaseException
) -> None:
    s = settings(monkeypatch, LLM_MAIN="openrouter:bad,nvidia:good", OPENROUTER_API_KEY="o", NVIDIA_API_KEY="n")
    primary, _primary_calls = _scripted("no", error)
    fallback, fallback_calls = _scripted("ok")
    built = iter((primary, fallback))

    def builder(**kw: Any) -> FakeListChatModel:
        del kw
        return next(built)

    caplog.set_level(logging.WARNING)
    model = LLMFactory(s, builder=builder).for_role(Role.MAIN)
    with pytest.raises(type(error)):
        model.invoke("hi")
    assert fallback_calls["n"] == 0
    assert not [r for r in caplog.records if r.getMessage() == "llm fallback"]


def test_missing_key_skipped_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    s = settings(
        monkeypatch,
        LLM_MAIN="openrouter:a,nvidia:b",
        LLM_BACKGROUND="openrouter:a,nvidia:b",
        LLM_GRAPH="openrouter:c,nvidia:b",
        NVIDIA_API_KEY="nvapi-test",
    )
    caplog.set_level(logging.WARNING)
    rec = Recorder()
    factory = LLMFactory(s, builder=rec)
    notes = [r for r in caplog.records if "skipping LLM provider" in r.getMessage()]
    assert len(notes) == 1
    assert "openrouter" in notes[0].getMessage()
    assert "OPENROUTER_API_KEY" in notes[0].getMessage()
    for role in Role:
        factory.for_role(role)
    assert {call["model"] for call in rec.calls} == {"b"}
    assert all(call["model_provider"] == "nvidia" for call in rec.calls)
    assert len([r for r in caplog.records if "skipping LLM provider" in r.getMessage()]) == 1


def test_missing_every_key_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="no usable LLM"):
        LLMFactory(settings(monkeypatch), builder=Recorder()).for_role(Role.MAIN)


def test_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, LLM_MAIN="acme:big")
    with pytest.raises(ConfigError, match="unknown LLM provider"):
        LLMFactory(s, builder=Recorder()).for_role(Role.MAIN)


def test_new_provider_is_a_registry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, LLM_MAIN="local:m")
    reg = {"local": ProviderSpec("local", "http://x/v1", None)}
    rec = Recorder()
    LLMFactory(s, providers=reg, builder=rec).for_role(Role.MAIN)
    assert rec.calls[0]["base_url"] == "http://x/v1"


def test_ollama_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, LLM_MAIN="ollama:qwen3", OLLAMA_BASE_URL="http://gpu:11434/v1")
    rec = Recorder()
    LLMFactory(s, builder=rec).for_role(Role.MAIN)
    assert rec.calls[0]["base_url"] == "http://gpu:11434/v1"
    assert not isinstance(LLMFactory(s, builder=rec).for_role(Role.MAIN), FallbackChatModel)


def test_real_clients_build_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    groq = settings(monkeypatch, LLM_MAIN="groq:openai/gpt-oss-20b", GROQ_API_KEY="gsk_x")
    assert type(LLMFactory(groq).chat(groq.model_for(Role.MAIN))).__name__ == "ChatOpenAI"
    nvidia = settings(monkeypatch, LLM_MAIN="nvidia:nvidia/nemotron-3-super-120b-a12b", NVIDIA_API_KEY="nvapi-test")
    assert type(LLMFactory(nvidia).chat(nvidia.model_for(Role.MAIN))).__name__ == "ChatNVIDIA"


def test_litellm_failure_logged_only_for_retryable_hops(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(logging.getLogger("nwb.dataflow"), "level", logging.INFO)
    record_litellm_failure(
        {
            "model": "openrouter/m",
            "fallbacks": ["nvidia_nim/m"],
            "exception": _http_error(openai.RateLimitError, 429),
            "metadata": {"nwb_role": "graph"},
        }
    )
    hops = [r for r in caplog.records if r.getMessage() == "llm fallback"]
    assert len(hops) == 1
    assert (hops[0].fields["provider"], hops[0].fields["role"]) == ("openrouter/m", "graph")  # type: ignore[attr-defined]
    caplog.clear()
    record_litellm_failure(
        {
            "model": "openrouter/m",
            "fallbacks": ["nvidia_nim/m"],
            "exception": _http_error(openai.AuthenticationError, 401),
        }
    )
    record_litellm_failure({"model": "openrouter/m", "exception": _http_error(openai.RateLimitError, 429)})
    assert not [r for r in caplog.records if r.getMessage() == "llm fallback"]


def test_graphiti_uses_the_fallback_client() -> None:
    models = (ROOT / "src" / "nwb" / "kg" / "models.py").read_text(encoding="utf-8")
    client = (ROOT / "src" / "nwb" / "kg" / "graphiti_llm.py").read_text(encoding="utf-8")
    assert "FallbackGraphitiClient" in models
    assert "class FallbackGraphitiClient(LLMClient)" in client
    assert "Graphiti cannot fall back" not in models
