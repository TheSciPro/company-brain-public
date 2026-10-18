"""GET /health probes. The process being up is separate from FalkorDB, Postgres and the LLM.

Nothing in the payload is a secret: no DSNs, hosts, passwords, or API keys. A failure is the exception
class name only. Results are cached for a few seconds so a platform health checker does not open a new
graph connection and an LLM request on every probe.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from nwb.config import Settings
from nwb.llm.factory import LLMFactory
from nwb.llm.providers import ProviderSpec
from nwb.llm.settings import LLMSettings
from nwb.obs.log import get_logger

log = get_logger(__name__)

PROBE_TIMEOUT_S = 3.0
PROBE_TTL_S = 5.0
_CACHE: dict[str, Any] = {"at": 0.0, "checks": None}

UrlOpen = Callable[..., Any]
Query = Callable[[], list[list[Any]]]
RowQuery = Callable[[], Any]


def health_payload(settings: Settings, checks: dict[str, Any]) -> dict[str, Any]:
    """The body of GET /health. ``status`` is the process, not the dependencies."""
    return {"status": "ok", "env": settings.app_env, "checks": checks}


def check_falkordb(settings: Settings, query: Query) -> dict[str, Any]:
    """One ``RETURN 1`` on the configured graph (server, or embedded FalkorDBLite when no host is set)."""
    mode = "server" if settings.falkor_target().host else "embedded"
    try:
        rows = query()
        reachable = bool(rows) and rows[0][0] == 1
        out: dict[str, Any] = {"reachable": reachable, "mode": mode}
        if not reachable:
            out["error"] = "unexpected_result"
        return out
    except Exception as exc:
        # FalkorDB rejects a read-only query until the graph key exists. The process is up; it is just empty.
        if type(exc).__name__ == "ResponseError" and "empty key" in str(exc):
            log.info("falkordb empty", mode=mode)
            return {"reachable": True, "mode": mode, "empty": True}
        log.warning("falkordb unreachable", error=type(exc).__name__, mode=mode)
        return {"reachable": False, "mode": mode, "error": type(exc).__name__}


def check_postgres(settings: Settings, fetch_ok: RowQuery) -> dict[str, Any]:
    """``SELECT 1`` when the SQL backend is Postgres. SQLite is reported as not configured."""
    if settings.db_backend != "postgres":
        return {"reachable": False, "configured": False, "backend": settings.db_backend}
    try:
        row = fetch_ok()
        reachable = isinstance(row, dict) and row.get("ok") == 1
        out = {"reachable": reachable, "configured": True, "backend": "postgres"}
        if not reachable:
            out["error"] = "unexpected_result"
        return out
    except Exception as exc:
        log.warning("postgres unreachable", error=type(exc).__name__)
        return {"reachable": False, "configured": True, "backend": "postgres", "error": type(exc).__name__}


def check_llm(
    settings: LLMSettings | None = None,
    *,
    urlopen: UrlOpen | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Whether the main chain's first usable endpoint answers. GET ``/models``, not a completion (no quota spend).

    ``LLM_MAIN`` is a provider chain. The probe takes the first link whose key is set, which is the one a chat
    would call first (links with no key are skipped). With no usable link it reports the first one.
    """
    llm = settings or LLMSettings()
    factory = LLMFactory(llm)
    first: dict[str, Any] | None = None
    for ref in llm.llm_main:
        base: dict[str, Any] = {"provider": ref.provider, "model": ref.model}
        try:
            spec, base_url = factory.provider(ref)
        except Exception as exc:
            log.warning("llm probe skipped", error=type(exc).__name__, provider=ref.provider)
            first = first or {**base, "reachable": False, "configured": False, "error": type(exc).__name__}
            continue
        if spec.key_field is not None and getattr(llm, spec.key_field) is None:
            first = first or {**base, "reachable": False, "configured": False, "reason": "key_missing"}
            continue
        return _probe(llm, spec, base_url, base, urlopen, timeout_s)
    return first or {"reachable": False, "configured": False, "reason": "no_model"}


def _probe(
    llm: LLMSettings,
    spec: ProviderSpec,
    base_url: str,
    base: dict[str, Any],
    urlopen: UrlOpen | None,
    timeout_s: float,
) -> dict[str, Any]:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/models", method="GET")  # noqa: S310 - provider URL from settings
    if spec.key_field is not None:
        secret = getattr(llm, spec.key_field)
        req.add_header("Authorization", f"Bearer {secret.get_secret_value()}")
    opener = urlopen or _open
    try:
        with opener(req, timeout=timeout_s) as resp:
            code = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
    except Exception as exc:
        log.warning("llm unreachable", error=type(exc).__name__, provider=spec.name)
        return {**base, "reachable": False, "configured": True, "error": type(exc).__name__}
    return {
        **base,
        "reachable": code < 500,
        "configured": True,
        "authenticated": 200 <= code < 300,
    }


def collect_checks(
    settings: Settings,
    *,
    falkor_query: Query,
    postgres_ok: RowQuery,
    llm: LLMSettings | None = None,
    urlopen: UrlOpen | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Run the three probes, reusing a short-lived cache."""
    tick = time.monotonic() if now is None else now
    cached = _CACHE["checks"]
    if cached is not None and tick - float(_CACHE["at"]) < PROBE_TTL_S:
        return dict(cached)
    checks = {
        "falkordb": check_falkordb(settings, falkor_query),
        "postgres": check_postgres(settings, postgres_ok),
        "llm": check_llm(llm, urlopen=urlopen),
    }
    _CACHE["at"] = tick
    _CACHE["checks"] = checks
    log.info(
        "health",
        falkordb=checks["falkordb"]["reachable"],
        postgres=checks["postgres"]["reachable"],
        llm=checks["llm"]["reachable"],
    )
    return checks


def reset_cache() -> None:
    """Test hook: drop the probe cache."""
    _CACHE["at"] = 0.0
    _CACHE["checks"] = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an HTTP answer, not a request we follow (the Authorization header must not travel)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(getattr(req, "full_url", newurl), code, msg, headers, fp)


def _open(req: urllib.request.Request, timeout: float = PROBE_TIMEOUT_S) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)
