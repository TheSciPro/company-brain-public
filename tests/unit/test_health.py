"""Health probes report reachability and never echo a DSN or a key."""

from __future__ import annotations

import json

from pydantic import SecretStr

from nwb.api.health import check_falkordb, check_llm, check_postgres, collect_checks, reset_cache
from nwb.config import Settings
from nwb.llm.settings import LLMSettings

SECRET = "sk-test-do-not-leak-123"
DSN = "postgresql://app:supersecret@db.example/nwb"


def setup_function() -> None:
    reset_cache()


def test_falkordb_empty_graph_is_reachable() -> None:
    body = check_falkordb(Settings(), _empty_graph)
    assert body["reachable"] is True
    assert body["empty"] is True
    assert "error" not in body
    assert "empty key" not in json.dumps(body)


def test_falkordb_embedded_ok_and_failure_hides_the_message() -> None:
    settings = Settings()
    assert check_falkordb(settings, lambda: [[1]])["reachable"] is True
    failed = check_falkordb(settings, _boom)
    assert failed["reachable"] is False
    assert failed["error"] == "RuntimeError"
    assert "supersecret" not in json.dumps(failed)


def test_postgres_unconfigured_on_sqlite_and_hides_dsn() -> None:
    sqlite = check_postgres(Settings(), lambda: {"ok": 1})
    assert sqlite == {"reachable": False, "configured": False, "backend": "sqlite"}
    postgres = Settings(database_url="postgresql://app@localhost/nwb")
    assert check_postgres(postgres, lambda: {"ok": 1})["reachable"] is True
    failed = check_postgres(postgres, _boom)
    blob = json.dumps(failed)
    assert "supersecret" not in blob
    assert "db.example" not in blob
    assert failed["error"] == "RuntimeError"


def test_llm_probe_does_not_return_the_key() -> None:
    settings = LLMSettings(llm_main="openrouter:vendor/model", openrouter_api_key=SecretStr(SECRET))
    seen: list[str] = []

    def opener(req: object, timeout: float = 0) -> _Resp:
        del timeout
        header = req.get_header("Authorization")  # type: ignore[attr-defined]
        seen.append(header or "")
        return _Resp()

    body = check_llm(settings, urlopen=opener)
    blob = json.dumps(body)
    assert SECRET not in blob
    assert body["reachable"] is True
    assert body["authenticated"] is True
    assert body["provider"] == "openrouter"
    assert seen and seen[0].startswith("Bearer ")
    assert SECRET in seen[0]


def test_llm_probe_skips_chain_links_without_a_key() -> None:
    """LLM_MAIN is a chain: the probe takes the first link with a key, as a chat would."""
    settings = LLMSettings(
        llm_main="nvidia:vendor/first,openrouter:vendor/model",
        nvidia_api_key=None,
        openrouter_api_key=SecretStr(SECRET),
    )
    urls: list[str] = []

    def opener(req: object, timeout: float = 0) -> _Resp:
        del timeout
        urls.append(req.full_url)  # type: ignore[attr-defined]
        return _Resp()

    body = check_llm(settings, urlopen=opener)
    assert body["provider"] == "openrouter"
    assert body["reachable"] is True
    assert len(urls) == 1


def test_missing_key_does_not_call_the_network() -> None:
    settings = LLMSettings(llm_main="openrouter:vendor/model", openrouter_api_key=None)

    def opener(req: object, timeout: float = 0) -> _Resp:
        del req, timeout
        raise AssertionError("should not call the network without a key")

    body = check_llm(settings, urlopen=opener)
    assert body["reason"] == "key_missing"
    assert body["reachable"] is False
    assert SECRET not in json.dumps(body)


def test_checks_are_cached() -> None:
    calls = {"n": 0}

    def query() -> list[list[int]]:
        calls["n"] += 1
        return [[1]]

    settings = Settings()
    collect_checks(settings, falkor_query=query, postgres_ok=lambda: {"ok": 1}, llm=_no_key(), now=10.0)
    collect_checks(settings, falkor_query=query, postgres_ok=lambda: {"ok": 1}, llm=_no_key(), now=12.0)
    assert calls["n"] == 1
    collect_checks(settings, falkor_query=query, postgres_ok=lambda: {"ok": 1}, llm=_no_key(), now=20.0)
    assert calls["n"] == 2


class ResponseError(Exception):
    """Stand-in for the Falkor client error. The probe matches the class name, not the import."""


def _empty_graph() -> list[list[int]]:
    raise ResponseError("Invalid graph operation on empty key")


def _boom() -> list[list[int]]:
    raise RuntimeError(f"connect failed {DSN}")


def _no_key() -> LLMSettings:
    return LLMSettings(llm_main="openrouter:vendor/model", openrouter_api_key=None)


class _Resp:
    status = 200

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False
