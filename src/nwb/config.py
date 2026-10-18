"""Settings from environment (12-factor). Names only in .env.example; never log values.

Profiles: ``APP_ENV=dev|test|prod``. Profile only changes *defaults* (log format, data-flow trace);
any explicit env var still wins. Unprefixed names (APP_ENV, DATABASE_URL, FALKORDB_URL, LOG_LEVEL,
DATAFLOW_TRACE) are accepted alongside the older ``NWB_*`` names.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "test", "prod"]
DbBackend = Literal["sqlite", "postgres"]


def _alias(name: str) -> AliasChoices:
    return AliasChoices(name.upper(), f"NWB_{name.upper()}")


def _dotenv_file() -> str | None:
    """``.env`` is for the app process. Tests set ``NWB_DISABLE_DOTENV=1`` before import so a dev URL cannot leak in."""
    if os.environ.get("NWB_DISABLE_DOTENV") == "1":
        return None
    return ".env"


class FalkorTarget(BaseSettings):
    """Where the graph lives. ``host`` empty -> embedded FalkorDBLite."""

    model_config = SettingsConfigDict(frozen=True)
    host: str = ""
    port: int = 6379
    username: str | None = None
    password: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NWB_",
        env_file=_dotenv_file(),
        env_ignore_empty=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_env: AppEnv = Field(default="dev", validation_alias=_alias("app_env"))

    # --- SQL -------------------------------------------------------------
    # Runtime DSN (the app role on Postgres). When set, it selects the backend and replaces sqlite_path / pg_dsn.
    database_url: str = Field(default="", validation_alias=_alias("database_url"))
    # Owner DSN for DDL, LangGraph setup(), ingest, and cross-user jobs. Empty = use the runtime DSN.
    migrate_url: str = Field(
        default="", validation_alias=AliasChoices("MIGRATE_URL", "NWB_MIGRATE_URL", "NEON_DIRECT_URL")
    )
    db_backend: DbBackend = "sqlite"
    sqlite_path: Path = Path("var/nwb.sqlite")
    pg_dsn: str = "postgresql://postgres@localhost:5433/nwb"
    pg_pool_min: int = 1
    pg_pool_max: int = 10
    pg_statement_timeout_ms: int = 5000
    # Pool hygiene for Neon, which closes idle connections (see nwb.db.pg_pool).
    pg_pool_check: bool = True  # validate a connection before handing it out; a dead one is replaced
    pg_pool_max_idle_s: float = 240.0  # below Neon's 5-minute idle suspend
    pg_pool_max_lifetime_s: float = 1800.0
    pg_keepalives_idle_s: int = 30
    pg_keepalives_interval_s: int = 10
    pg_keepalives_count: int = 3
    pg_connect_timeout_s: int = 10
    # The same for FalkorDB Cloud: an idle TCP connection can be dropped upstream ("Operation timed out").
    falkor_keepalive: bool = True
    falkor_health_check_s: int = 30  # PING a connection idle this long before using it; 0 turns it off
    falkor_connect_timeout_s: float = 10.0
    falkor_retries: int = 1  # reconnect and retry a command once on a connection or timeout error

    # --- Graph -----------------------------------------------------------
    falkordb_url: str = Field(default="", validation_alias=_alias("falkordb_url"))  # redis://user:pass@host:port
    falkor_host: str = ""
    falkor_port: int = 6379
    falkor_username: str | None = None
    falkor_password: SecretStr | None = None
    falkor_lite_path: Path = Path("var/falkor.db")
    graph_name: str = "nw"
    graph_query_timeout_ms: int = 2000
    # Prepended to ``{company}_kb_{level}`` so a run can use its own graphs. Empty uses the names as loaded.
    graph_prefix: str = Field(default="", validation_alias=_alias("graph_prefix"))

    # --- Observability ---------------------------------------------------
    log_level: str = Field(default="", validation_alias=_alias("log_level"))  # "" -> DEBUG dev, INFO otherwise
    log_format: Literal["", "console", "json"] = Field(default="", validation_alias=_alias("log_format"))
    dataflow_trace: bool | None = Field(default=None, validation_alias=_alias("dataflow_trace"))  # None -> on in dev
    log_dir: Path = Path("var/logs")
    log_file: bool | None = Field(default=None, validation_alias=_alias("log_file"))  # None -> on in dev (app.jsonl)
    log_quiet: str = Field(  # loggers held at WARNING (comma-separated)
        default=(
            "httpx,httpcore,openai,urllib3,litellm,LiteLLM,LiteLLM Router,LiteLLM Proxy,graphiti_core,neo4j,"
            "apscheduler,multipart,watchfiles,uvicorn.access,sse_starlette"
        ),
        validation_alias=_alias("log_quiet"),
    )

    # --- Data ------------------------------------------------------------
    data_dir: Path = Path("data")
    blob_dir: Path = Path("var/blobs")
    migrations_dir: Path = Path("migrations")
    company_id: str = "NW"  # tenant identity (deployment setting, overridden per deployment via env)
    company_name: str = "Northwind Consulting"
    extraction_mode: Literal["llm", "cache_only", "off"] = "cache_only"  # `nwb extract` records with "llm"
    extraction_cache_dir: Path = Path("var/extractions")
    # Directive phrasing (edit -> USER.md). live calls the model; record stores the call; replay never calls out.
    directive_record_mode: Literal["live", "record", "replay"] = "live"
    directive_record_dir: Path = Path("var/directive_phrases")

    # --- Agent runtime (tunables live in YAML under config_dir; prompts are files) ---
    config_dir: Path = Path("config")
    prompts_dir: Path = Path("prompts")
    agent_dir: Path = Path("agent")  # AGENTS.md, skills/, subagents/, templates/ (markdown assets, no code)
    personas_file: Path = Path("demo/personas.yaml")  # demo fixture: UI persona switcher, `nwb chat --as`
    # LangGraph persistence for the agent: checkpointer (threads) + store (/memories/ files). "auto" follows the SQL
    # backend: Postgres (Neon in prod via DATABASE_URL) -> PostgresSaver + PostgresStore; SQLite -> SQLite both.
    checkpointer: Literal["auto", "memory", "sqlite", "postgres"] = "auto"
    checkpoint_path: Path = Path("var/checkpoints.sqlite")
    store_path: Path = Path("var/agent_store.sqlite")
    api_cors_origins: str = "http://localhost:3000,http://localhost:5173"

    def __init__(self, **values: object) -> None:
        # Init kwargs are merged with env before validators run. Pin an empty URL here, while we can still
        # see the caller's kwargs, so an ambient DATABASE_URL cannot replace an explicit sqlite path or DSN.
        if "database_url" not in values and any(key in values for key in ("sqlite_path", "pg_dsn", "db_backend")):
            values = {**values, "database_url": ""}
        super().__init__(**values)

    @model_validator(mode="after")
    def _apply_profile(self) -> Settings:
        if not self.log_level:
            self.log_level = "DEBUG" if self.app_env == "dev" else "INFO"
        if not self.log_format:
            self.log_format = "json" if self.app_env == "prod" else "console"
        if self.dataflow_trace is None:
            self.dataflow_trace = self.app_env == "dev"
        if self.log_file is None:
            self.log_file = self.app_env == "dev"
        if self.database_url:
            self._apply_database_url(self.database_url)
        return self

    def _apply_database_url(self, url: str) -> None:
        scheme = urlparse(url).scheme
        if scheme == "sqlite":
            self.db_backend = "sqlite"
            self.sqlite_path = Path(url.split("sqlite:///", 1)[-1])
        elif scheme in {"postgres", "postgresql"}:
            self.db_backend = "postgres"
            self.pg_dsn = url
        else:
            raise ValueError(f"unsupported DATABASE_URL scheme {scheme!r} (use sqlite:/// or postgresql://)")

    @property
    def agent_persistence(self) -> Literal["memory", "sqlite", "postgres"]:
        if self.checkpointer != "auto":
            return self.checkpointer
        return "postgres" if self.db_backend == "postgres" else "sqlite"

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    def falkor_target(self) -> FalkorTarget:
        if self.falkordb_url:
            u = urlparse(self.falkordb_url)
            pw = SecretStr(unquote(u.password)) if u.password else None
            return FalkorTarget(
                host=u.hostname or "", port=u.port or 6379, username=unquote(u.username or "") or None, password=pw
            )
        return FalkorTarget(
            host=self.falkor_host, port=self.falkor_port, username=self.falkor_username, password=self.falkor_password
        )


def graph_name_prefix(cli: str | None, configured: str) -> str:
    """Prefix shared by the company-graph loader and the running app.

    ``cli`` is ``--prefix``. Omit it (``None``) to use the deployment setting
    (``GRAPH_PREFIX`` / ``NWB_GRAPH_PREFIX``). An empty string is no prefix, so
    a default load and a default app name the same ``{company}_kb_{level}`` graphs.
    """
    if cli is None:
        return configured.strip()
    return cli.strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
