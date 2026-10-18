"""Production refuses to start jobs that would silently see no rows."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nwb.config import Settings
from nwb.container import Container, require_job_owner
from nwb.errors import ConfigError


def test_prod_postgres_without_owner_url_fails(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(  # type: ignore[call-arg]
        app_env="prod",
        db_backend="postgres",
        pg_dsn="postgresql://nw_app@127.0.0.1:1/none",
        _env_file=None,
    )
    with caplog.at_level(logging.ERROR, logger="nwb.container"), pytest.raises(ConfigError, match="NWB_MIGRATE_URL"):
        Container.from_settings(settings)
    assert "NWB_MIGRATE_URL" in caplog.text


def test_prod_postgres_with_a_distinct_owner_url_passes_the_check() -> None:
    settings = Settings(  # type: ignore[call-arg]
        app_env="prod",
        db_backend="postgres",
        pg_dsn="postgresql://nw_app@db.example/app",
        migrate_url="postgresql://owner@db.example/app",
        _env_file=None,
    )
    require_job_owner(settings)


def test_prod_sqlite_does_not_require_an_owner_url(tmp_path: Path) -> None:
    settings = Settings(app_env="prod", sqlite_path=tmp_path / "t.sqlite", _env_file=None)  # type: ignore[call-arg]
    container = Container.from_settings(settings)
    container.close()
