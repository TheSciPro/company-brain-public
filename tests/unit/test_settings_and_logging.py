"""Profiles, backend URLs, redaction and the dev data-flow trace."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from nwb.config import Settings
from nwb.obs.log import dataflow, new_trace, scrub, scrub_str
from nwb.obs.log import setup_logging as configure_logging

ENV = (
    "APP_ENV",
    "DATABASE_URL",
    "FALKORDB_URL",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DATAFLOW_TRACE",
    "NWB_DB_BACKEND",
    "NWB_FALKOR_HOST",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def mk(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = mk(monkeypatch)
    assert (s.app_env, s.log_level, s.log_format, s.dataflow_trace, s.db_backend) == (
        "dev",
        "DEBUG",
        "console",
        True,
        "sqlite",
    )


def test_prod_profile_and_neon_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql://app:pw@ep-x.neon.tech/nwb?sslmode=require"
    s = mk(monkeypatch, APP_ENV="prod", DATABASE_URL=url)
    assert (s.log_level, s.log_format, s.dataflow_trace) == ("INFO", "json", False)
    assert (s.db_backend, s.pg_dsn) == ("postgres", url)


def test_explicit_flag_beats_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mk(monkeypatch, APP_ENV="dev", DATAFLOW_TRACE="false").dataflow_trace is False


def test_sqlite_url(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mk(monkeypatch, DATABASE_URL="sqlite:///var/x.sqlite").sqlite_path == Path("var/x.sqlite")


def test_bad_url(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        mk(monkeypatch, DATABASE_URL="mysql://x")


def test_explicit_sqlite_path_ignores_ambient_database_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://env-user@db.example/dev")
    s = Settings(sqlite_path=tmp_path / "hermetic.sqlite", _env_file=None)  # type: ignore[call-arg]
    assert s.db_backend == "sqlite"
    assert s.sqlite_path == tmp_path / "hermetic.sqlite"


def test_explicit_pg_dsn_ignores_ambient_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://env-user@db.example/dev")
    s = Settings(pg_dsn="postgresql://local/test", db_backend="postgres", _env_file=None)  # type: ignore[call-arg]
    assert (s.db_backend, s.pg_dsn) == ("postgres", "postgresql://local/test")


def test_dotenv_is_not_read_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://from-file@db.example/dev\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings()  # env_file was frozen to None at import because NWB_DISABLE_DOTENV=1
    assert s.db_backend == "sqlite"
    assert "from-file" not in s.pg_dsn


def test_env_file_database_url_still_overrides_sqlite_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "prod.env"
    url = "postgresql://app@db.example/prod"
    env.write_text(f"DATABASE_URL={url}\nNWB_SQLITE_PATH={tmp_path / 'ignored.sqlite'}\n")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NWB_SQLITE_PATH", raising=False)
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert (s.db_backend, s.pg_dsn) == ("postgres", url)


def test_falkor_url_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    t = mk(monkeypatch, FALKORDB_URL="redis://falkordb:s%40cret@r-1.falkordb.cloud:55123").falkor_target()
    assert (t.host, t.port, t.username) == ("r-1.falkordb.cloud", 55123, "falkordb")
    assert t.password is not None and t.password.get_secret_value() == "s@cret"
    assert "s@cret" not in repr(t)


def test_falkor_lite_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mk(monkeypatch).falkor_target().host == ""


def test_redaction() -> None:
    assert scrub_str("key gsk_abcdefghijk1234") == "key [redacted]"
    assert "pw" not in scrub_str("postgresql://app:pw@host/db")
    out = scrub({"api_key": "x", "content": "hello", "n": 3}, keep_content=False)
    assert out == {"api_key": "[redacted]", "content": "[5 chars]", "n": 3}


def test_dev_dataflow_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = mk(monkeypatch)
    s.log_dir = tmp_path
    configure_logging(s)
    try:
        with new_trace("t-1"):
            dataflow("sql.write", table="people", rows=2, content="Asha", token="gsk_abcdefghijk")
        logging.getLogger("other").info("not in trace file")
    finally:
        _reset()
    lines = [json.loads(x) for x in (tmp_path / "dataflow.jsonl").read_text().splitlines()]
    assert len(lines) == 1
    e = lines[0]
    assert (e["hop"], e["trace_id"], e["rows"], e["content"], e["token"]) == (
        "sql.write",
        "t-1",
        2,
        "Asha",
        "[redacted]",
    )


def test_prod_json_hides_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    s = mk(monkeypatch, APP_ENV="prod")
    s.log_dir = tmp_path
    configure_logging(s)
    try:
        with new_trace("t-2"):
            logging.getLogger("nwb.x").info("turn done", extra={"fields": {"content": "secret plan", "n": 1}})
            dataflow("llm.call", model="m")  # DEBUG: dropped in prod
    finally:
        _reset()
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert out == [{**out[0], "msg": "turn done", "trace_id": "t-2", "content": "[11 chars]", "n": 1}]
    assert not (tmp_path / "dataflow.jsonl").exists()


def _reset() -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_nwb_handler", False)]:
        root.removeHandler(h)
        h.close()
