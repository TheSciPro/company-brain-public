"""Log LiteLLM fallback hops (GraphRAG-SDK) through the same data-flow event as LangChain.

LiteLLM decides whether the next model actually runs. We record a hop only when the
failing call still has ``fallbacks`` configured and the error is 429, 5xx, a timeout
or a connection failure — the same rule as ``with_fallbacks``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nwb.llm.fallback import is_retryable, log_fallback, reason
from nwb.obs.log import get_logger

log = get_logger(__name__)

try:
    import litellm
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # GraphRAG-SDK optional extra; the LangChain chain does not need it
    litellm = None
    CustomLogger = object

_INSTALLED = False


def _metadata(kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    params = kwargs.get("litellm_params")
    if isinstance(params, Mapping):
        raw = params.get("metadata")
        if isinstance(raw, Mapping):
            return raw
    raw = kwargs.get("metadata")
    return raw if isinstance(raw, Mapping) else {}


def record_litellm_failure(kwargs: Mapping[str, Any]) -> None:
    """Log one hop when this LiteLLM call failed and still had fallbacks left."""
    fallbacks = kwargs.get("fallbacks")
    params = kwargs.get("litellm_params")
    if not fallbacks and isinstance(params, Mapping):
        fallbacks = params.get("fallbacks")
    if not fallbacks:
        return
    exc = kwargs.get("exception")
    if not isinstance(exc, BaseException) or not is_retryable(exc):
        return
    log_fallback(
        provider=str(kwargs.get("model") or ""),
        reason=reason(exc),
        role=str(_metadata(kwargs).get("nwb_role") or ""),
        model=str(kwargs.get("model") or ""),
    )


if litellm is not None:

    class _HopLog(CustomLogger):  # type: ignore[misc]
        def log_failure_event(self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
            del response_obj, start_time, end_time
            try:
                record_litellm_failure(kwargs)
            except Exception:
                log.warning("litellm fallback log failed", exc_info=True)

        async def async_log_failure_event(
            self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
        ) -> None:
            self.log_failure_event(kwargs, response_obj, start_time, end_time)


def install_litellm_fallback_log() -> None:
    """Register the hop logger once. No-op when LiteLLM is not installed."""
    global _INSTALLED
    if _INSTALLED or litellm is None:
        return
    litellm.callbacks.append(_HopLog())
    _INSTALLED = True
