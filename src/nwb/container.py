"""Composition root: the only place that picks concrete adapters (dependency injection by hand).

Everything is built once, lazily, from ``Settings`` + ``config/*.yaml`` + ``prompts/*.md`` + ``agent/``.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import CompositeBackend
from falkordb import FalkorDB as FalkorServer
from langchain_core.tools import BaseTool
from redislite.falkordb_client import FalkorDB as FalkorLite

from nwb.access.visible import access_policy_for
from nwb.actions.models import ActionView
from nwb.actions.service import EXECUTORS, ActionService, Executor
from nwb.agent.backends import build_backend
from nwb.agent.clock import Clock, SystemClock
from nwb.agent.graph import AgentAssembly, Models, build_agent
from nwb.agent.memory_files import MemoryFiles
from nwb.agent.middleware.audit import ToolAuditMiddleware
from nwb.agent.middleware.bootstrap import BootstrapMiddleware
from nwb.agent.middleware.excluded import ExcludedToolsMiddleware
from nwb.agent.middleware.runtime_context import RuntimeBlockBuilder, RuntimeContextMiddleware
from nwb.agent.persistence import AgentPersistence, build_persistence
from nwb.agent.runner import ChatRunner
from nwb.agent.tools.actions import ActionTools
from nwb.agent.tools.knowledge import KnowledgeTools
from nwb.agent.tools.onboarding import OnboardingTools
from nwb.blob.local import LocalBlobStore
from nwb.conf import AgentConfig, ConfigLoader, KnowledgeConfig, MemoryConfig, PersonasConfig, SourcesConfig
from nwb.conf.models import ActionsConfig, ClassificationConfig, GraphConfig, HeartbeatConfig, OnboardingConfig
from nwb.config import Settings
from nwb.connectors.agents import ConnectorBindings, SourceAgents, build_source_agents
from nwb.db.database import PostgresDatabase, SqliteDatabase
from nwb.db.models import Company
from nwb.db.pg_pool import PoolTuning
from nwb.errors import ConfigError
from nwb.graph.client import FalkorGraphStore, server_kwargs
from nwb.ingest.layer1 import Layer1Loader
from nwb.ingest.layer2 import Layer2Loader
from nwb.ingest.sources import DirectorySource, HrisSource
from nwb.jobs.consolidate import Consolidation
from nwb.jobs.graph_sync import GraphSync
from nwb.jobs.heartbeat import HeartbeatJob
from nwb.jobs.watcher import WatcherJob
from nwb.kg.models import GraphModels
from nwb.kg.ontology import OntologyLifecycle
from nwb.kg.recording import CallRecorder, RecordMode
from nwb.kg.server import FalkorEndpoint, LocalFalkorServer, resolve_company_falkor
from nwb.knowledge.approvals import ApprovalMatrix
from nwb.knowledge.audit import DecisionAuditor
from nwb.knowledge.extract import CachedExtractor, ExtractionCache, FactExtractor, LLMFactExtractor, structured_invoke
from nwb.knowledge.retrieve import build_company_retriever
from nwb.knowledge.search import CompanySearch
from nwb.knowledge.service import KnowledgeService
from nwb.llm import LLMFactory, LLMSettings
from nwb.llm.settings import Role
from nwb.memory.consolidate import apply_change
from nwb.memory.edit_learning import EditLearning
from nwb.memory.embeddings import Embedder, resolve_embedder
from nwb.memory.guards import (
    LengthGuard,
    MemoryWriteGate,
    QualityGate,
    QualityGuard,
    QuoteGuard,
    SecretGuard,
    WriteGuard,
)
from nwb.memory.phrasing import LLMPhraser, PhraseRecorder
from nwb.memory.store import PersonalMemory
from nwb.obs.log import get_logger
from nwb.onboarding.bootstrap import OnboardingBootstrap
from nwb.onboarding.service import OnboardingService
from nwb.ports import AccessPolicy, SqlDatabase, UnitOfWork
from nwb.prompts import PromptLibrary
from nwb.temporal.graph_facts import GraphFactBook

log = get_logger(__name__)


def require_job_owner(s: Settings) -> None:
    """Prod jobs that scan across users need the owner URL. The app role would otherwise see no rows."""
    if not s.is_prod or s.db_backend != "postgres":
        return
    if s.migrate_url and s.migrate_url != s.pg_dsn:
        return
    log.error("refusing to start: background jobs need NWB_MIGRATE_URL set to the database owner")
    raise ConfigError(
        "NWB_MIGRATE_URL must be the database owner URL in production. "
        "Without it, ingest, graph sync, heartbeat, the watcher and post-session see no rows."
    )


def build_database(s: Settings, *, dsn: str | None = None, require_app_role: bool = False) -> SqlDatabase:
    if s.db_backend == "postgres":
        return PostgresDatabase(
            dsn or s.pg_dsn,
            min_size=s.pg_pool_min,
            max_size=s.pg_pool_max,
            statement_timeout_ms=s.pg_statement_timeout_ms,
            require_app_role=require_app_role,
            tuning=PoolTuning.from_settings(s),
        )
    return SqliteDatabase(s.sqlite_path)


def open_for_migrate(s: Settings) -> SqlDatabase:
    """Owner connection for DDL. Uses ``migrate_url`` when it is set, otherwise the runtime DSN."""
    if s.db_backend == "postgres" and s.migrate_url:
        return build_database(s, dsn=s.migrate_url)
    return build_database(s)


@dataclass
class Container:
    settings: Settings
    db: SqlDatabase
    admin_db: SqlDatabase | None = None
    connector_bindings: ConnectorBindings = field(default_factory=ConnectorBindings)
    _graph: FalkorGraphStore | None = None
    _falkor_client: Any = None
    _company_client: Any = None
    _company_server: LocalFalkorServer | None = None
    _company_endpoint_value: FalkorEndpoint | None = None
    _keepalive: list[Any] = field(default_factory=list)

    @classmethod
    def from_settings(cls, s: Settings) -> Container:
        require_job_owner(s)
        app = build_database(s, require_app_role=s.is_prod)
        admin: SqlDatabase | None = None
        if s.db_backend == "postgres" and s.migrate_url and s.migrate_url != s.pg_dsn:
            admin = build_database(s, dsn=s.migrate_url)
        return cls(settings=s, db=app, admin_db=admin)

    @property
    def maintenance_db(self) -> SqlDatabase:
        """Owner pool for ingest and cross-user jobs. The app pool, when only one URL is configured."""
        return self.admin_db if self.admin_db is not None else self.db

    # ================================================================ infrastructure
    def _falkor(self) -> Any:
        """The org graph (``nw``). A configured host, otherwise the embedded file beside SQLite."""
        if self._falkor_client is None:
            s = self.settings
            t = s.falkor_target()
            if t.host:
                pw = t.password.get_secret_value() if t.password else None
                client = FalkorServer(
                    host=t.host, port=t.port, username=t.username, password=pw, **server_kwargs(self.settings)
                )
            else:
                s.falkor_lite_path.parent.mkdir(parents=True, exist_ok=True)
                client = FalkorLite(str(s.falkor_lite_path))
            self._keepalive.append(client)
            self._falkor_client = client
        return self._falkor_client

    def _company_falkor(self) -> Any:
        """FalkorDB for the per-level company graphs, including facts.

        Same resolution as company search: ``resolve_company_falkor`` (a URL, then
        ``falkor_target()`` when it names a host, otherwise ``graph.yaml`` ``server.local_dir``).
        """
        if self._company_client is None:
            target = resolve_company_falkor(self.settings, self.graph_config.server)
            endpoint = target.endpoint
            if endpoint is None:
                if target.local_dir is None:
                    raise ConfigError("company FalkorDB target has neither an endpoint nor a local directory")
                server = LocalFalkorServer(target.local_dir, port=target.local_port, persist=target.persist)
                server.start()
                atexit.register(server.stop)
                self._company_server = server
                endpoint = FalkorEndpoint(host=server.host, port=server.port)
            self._company_endpoint_value = endpoint
            self._company_client = FalkorServer(
                host=endpoint.host,
                port=endpoint.port,
                username=endpoint.username,
                password=endpoint.password,
                ssl=endpoint.ssl,
                **server_kwargs(self.settings),
            )
        return self._company_client

    def _company_endpoint(self) -> FalkorEndpoint:
        """The endpoint ``_company_falkor`` opened, so search does not start a second server."""
        self._company_falkor()
        if self._company_endpoint_value is None:
            raise ConfigError("company FalkorDB endpoint was not opened")
        return self._company_endpoint_value

    @property
    def graph(self) -> FalkorGraphStore:
        if self._graph is None:
            s = self.settings
            self._graph = FalkorGraphStore(self._falkor().select_graph(s.graph_name), s.graph_query_timeout_ms)
        return self._graph

    @cached_property
    def fact_book(self) -> GraphFactBook:
        levels = tuple(level.code for level in self.classification.levels)
        return GraphFactBook(
            self._company_falkor(),
            self.graph_config,
            levels,
            self.settings.company_id,
            self.settings.graph_prefix,
            self.graph_config.company.facts.query_timeout_ms,
        )

    def access_policy(self) -> AccessPolicy:
        return access_policy_for(self.db)

    def graph_sync(self) -> GraphSync:
        return GraphSync(self.maintenance_db, self.graph)

    def _sync_graph(self) -> None:
        self.graph_sync().drain()

    @cached_property
    def config(self) -> ConfigLoader:
        return ConfigLoader(self.settings.config_dir)

    @cached_property
    def agent_config(self) -> AgentConfig:
        return self.config.load("agent", AgentConfig)

    @cached_property
    def memory_config(self) -> MemoryConfig:
        return self.config.load("memory", MemoryConfig)

    @cached_property
    def sources(self) -> SourcesConfig:
        return self.config.load("sources", SourcesConfig)

    @cached_property
    def knowledge_config(self) -> KnowledgeConfig:
        return self.config.load("knowledge", KnowledgeConfig)

    @cached_property
    def classification(self) -> ClassificationConfig:
        return self.config.load("classification", ClassificationConfig)

    @cached_property
    def graph_config(self) -> GraphConfig:
        return self.config.load("graph", GraphConfig)

    @cached_property
    def onboarding_config(self) -> OnboardingConfig:
        return self.config.load("onboarding", OnboardingConfig)

    @cached_property
    def heartbeat_config(self) -> HeartbeatConfig:
        return self.config.load("heartbeat", HeartbeatConfig)

    @cached_property
    def actions_config(self) -> ActionsConfig:
        return self.config.load("actions", ActionsConfig)

    @cached_property
    def personas(self) -> PersonasConfig:
        return ConfigLoader(self.settings.personas_file.parent).load(self.settings.personas_file.stem, PersonasConfig)

    @cached_property
    def prompts(self) -> PromptLibrary:
        return PromptLibrary(self.settings.prompts_dir)

    @cached_property
    def clock(self) -> Clock:
        return SystemClock(self.agent_config.timezone)

    @cached_property
    def llm(self) -> LLMFactory:
        return LLMFactory(LLMSettings())

    def chat_model(self, role: Role) -> Any:
        return self.llm.for_role(role)

    @cached_property
    def embedder(self) -> Embedder:
        return resolve_embedder(self.memory_config.embedder)

    # ================================================================ ingest
    def layer1_loader(self) -> Layer1Loader:
        s = self.settings
        return Layer1Loader(
            self.maintenance_db,
            DirectorySource(s.data_dir),
            HrisSource(s.data_dir),
            Company(id=s.company_id, name=s.company_name),
            self.sources.directory,
        )

    def extractor(self) -> FactExtractor | None:
        """LLM fact extraction behind a content-hash cache. ``cache_only`` (tests, CI) never calls a model."""
        s = self.settings
        if s.extraction_mode == "off":
            return None
        live: FactExtractor | None = None
        if s.extraction_mode == "llm":
            ec = self.knowledge_config.extraction
            role = Role.MAIN if ec.role == "main" else Role.BACKGROUND
            refs = self.llm.chain(role)
            live = LLMFactExtractor(
                structured_invoke(self.llm.for_role(role)), self.prompts, ec, ",".join(str(r) for r in refs)
            )
        return CachedExtractor(ExtractionCache(s.extraction_cache_dir), live)

    def layer2_loader(self) -> Layer2Loader:
        s = self.settings
        return Layer2Loader(
            self.maintenance_db,
            s.data_dir,
            LocalBlobStore(s.blob_dir),
            s.company_id,
            self.sources,
            self.extractor(),
            s.company_name,
            self.sources.directory.root_unit,
            self.fact_book,
            self.classification.unknown_tag.level,
        )

    # ================================================================ domain services
    def write_guards(self) -> list[WriteGuard]:
        """Episode chunks and ``/memories/facts/`` writes. ``QualityGuard`` is the noise gate."""
        mc = self.memory_config
        return [
            LengthGuard(mc.content_max_chars),
            SecretGuard(mc.secret_patterns),
            QualityGuard(QualityGate(mc.quality)),
            QuoteGuard(),
        ]

    def profile_gate(self) -> MemoryWriteGate:
        """``USER.md`` / ``MEMORY.md`` only. The same quality rules, without the one-fact length cap."""
        mc = self.memory_config
        return MemoryWriteGate(SecretGuard(mc.secret_patterns), QualityGate(mc.quality))

    @cached_property
    def personal_memory(self) -> PersonalMemory:
        """Graphiti on FalkorDB. Built on first use so tests can inject a double before this runs.

        ``graphiti-core`` is an optional extra. Importing it here keeps ``import nwb.container`` working
        without that extra, and keeps the agent suite off Falkor until something actually reads memory.
        """
        from nwb.kg.models import GraphModels
        from nwb.kg.server import FalkorEndpoint, LocalFalkorServer
        from nwb.memory.graphiti_memory import open_personal_memory

        s = self.settings
        gc = self.graph_config
        if s.falkordb_url:
            endpoint = FalkorEndpoint.from_url(s.falkordb_url)
        else:
            server = LocalFalkorServer(s.config_dir.parent / gc.server.local_dir, persist=gc.server.persist)
            server.start()
            self._keepalive.append(server)
            endpoint = FalkorEndpoint(host=server.host, port=server.port)
        models = GraphModels(self.llm, gc, s.config_dir.parent)
        llm = models.graphiti_llm(models.recorder())
        return open_personal_memory(endpoint, self.embedder, llm, gc, self.memory_config, self.write_guards())

    @cached_property
    def auditor(self) -> DecisionAuditor:
        return DecisionAuditor(self.db)

    @property
    def stopwords(self) -> frozenset[str]:
        return self.memory_config.search.stopwords

    @cached_property
    def knowledge(self) -> KnowledgeService:
        return KnowledgeService(
            self.db,
            self.access_policy(),
            self.sources,
            self.knowledge_config,
            self.embedder,
            self.stopwords,
            self.auditor,
            self.settings.company_id,
            self.fact_book,
        )

    @cached_property
    def approvals(self) -> ApprovalMatrix:
        return ApprovalMatrix(self.db, self.sources, self.knowledge_config.approvals, self.auditor, self.stopwords)

    @cached_property
    def company_search(self) -> CompanySearch:
        return CompanySearch(
            self.db,
            self.access_policy(),
            self.classification,
            self.graph_config,
            self.knowledge_config.search,
            build_company_retriever(self.settings, self.graph_config, self.embedder, endpoint=self._company_endpoint()),
            prefix=self.settings.graph_prefix,
        )

    @cached_property
    def onboarding(self) -> OnboardingService:
        return OnboardingService(self.db, self.onboarding_config, self.settings.company_id)

    @cached_property
    def maintenance_onboarding(self) -> OnboardingService:
        """Template seed and plan creation scan every hire. They run on the owner connection."""
        return OnboardingService(self.maintenance_db, self.onboarding_config, self.settings.company_id)

    @cached_property
    def actions(self) -> ActionService:
        return ActionService(self.db, self.actions_config, executors=self._action_executors())

    def _action_executors(self) -> dict[str, Executor]:
        def apply_ontology(_uow: UnitOfWork, action: ActionView) -> str:
            return self.ontology.apply_approved(action)

        return {**EXECUTORS, "memory_apply": self._apply_memory_change, "apply_ontology": apply_ontology}

    def _apply_memory_change(self, _uow: UnitOfWork, action: ActionView) -> str:
        """Approved memory patch. A removal is refused inside apply_change: nothing is deleted."""
        return apply_change(
            self.memory_files, self.personal_memory, self.profile_gate(), self.settings.company_id, action
        )

    @cached_property
    def ontology(self) -> OntologyLifecycle:
        life = self.graph_config.company.ontology
        groups = (
            self.sources.directory.executive_groups
            if life.reviewer_role == "executive"
            else self.sources.directory.leader_groups
        )
        return OntologyLifecycle(self.db, self.actions, self.graph_config, groups, self.settings.config_dir.parent)

    def graph_sdk_llm(self) -> Any:
        """GraphRAG-SDK model for ``LLM_GRAPH`` (else the background model). Calls are recorded under ``var/``."""
        root = self.settings.config_dir.parent
        recorder = CallRecorder(root / "var" / "llm_calls" / "ontology", RecordMode.RECORD)
        return GraphModels(self.llm, self.graph_config, root).sdk_llm(recorder)

    @cached_property
    def watcher(self) -> WatcherJob:
        return WatcherJob(self.maintenance_db, self.actions, self.actions_config, loader=self.config)

    @cached_property
    def background_model(self) -> Any:
        return self.chat_model(Role.BACKGROUND)

    @cached_property
    def heartbeat(self) -> HeartbeatJob:
        def composer(prompt: str) -> str:
            # The job calls this only while heartbeat.yaml use_llm is true; the model is built on first use,
            # so switching use_llm on needs no restart and an install with no LLM key still starts.
            return str(self.background_model.invoke(prompt).content)

        return HeartbeatJob(
            self.maintenance_db,
            self.onboarding,
            self.prompts,
            self.heartbeat_config,
            composer,
            actions=self.actions,
            timezone=self.agent_config.timezone,
            loader=self.config,
        )

    # ================================================================ agent
    @cached_property
    def tools(self) -> dict[str, BaseTool]:
        """Every domain tool by name. Which ones the model sees is decided by config + SKILL.md, not here."""
        packs = [
            KnowledgeTools(self.db, self.knowledge, self.approvals, self.company_search).tools(),
            OnboardingTools(self.onboarding).tools(),
            ActionTools(self.actions, hitl_gated="propose_action" in self.agent_config.interrupt_on).tools(),
        ]
        out: dict[str, BaseTool] = {}
        for t in (t for pack in packs for t in pack):
            if t.name in out:
                raise ValueError(f"duplicate tool name {t.name}")
            out[t.name] = t
        return out

    @cached_property
    def persistence(self) -> AgentPersistence:
        return build_persistence(self.settings)

    @cached_property
    def backend(self) -> CompositeBackend:
        return build_backend(self.settings.agent_dir, self.personal_memory, self.profile_gate())

    @cached_property
    def memory_files(self) -> MemoryFiles:
        return MemoryFiles(self.persistence.store, self.db, self.settings.agent_dir / "templates")

    @cached_property
    def edit_learning(self) -> EditLearning:
        cfg = self.memory_config.edit_learning
        role = Role.MAIN if cfg.role == "main" else Role.BACKGROUND
        recorder = PhraseRecorder(self.settings.directive_record_dir, self.settings.directive_record_mode)
        phraser = LLMPhraser(self.chat_model(role), self.prompts, cfg, recorder)
        return EditLearning(self.memory_files, phraser, cfg, self.profile_gate())

    @cached_property
    def models(self) -> Models:
        return Models(self.chat_model(Role.MAIN), self.chat_model(Role.BACKGROUND))

    def agent_middleware(self) -> list[Any]:
        ac = self.agent_config
        builder = RuntimeBlockBuilder(
            self.db,
            self.personal_memory,
            self.prompts,
            ac.payload,
            self.clock,
            ac.timezone,
            self.settings.company_name,
            ac.prompts.runtime,
        )
        boot = OnboardingBootstrap(self.db, self.onboarding, self.prompts, ac.prompts.bootstrap, self.onboarding_config)
        return [
            RuntimeContextMiddleware(builder),
            BootstrapMiddleware(boot),
            ToolAuditMiddleware(self.db),
        ]

    def source_agents(self) -> SourceAgents:
        """One subagent per source in ``config/sources.yaml``. Built at agent assembly, not per turn."""
        cfg = self.agent_config
        permissions = [
            FilesystemPermission(operations=list(rule.operations), paths=list(rule.paths), mode=rule.mode)
            for rule in cfg.permissions
        ]
        return build_source_agents(
            self.sources,
            self.db,
            self.access_policy(),
            LocalBlobStore(self.settings.blob_dir),
            self.connector_bindings,
            self.memory_config.search,
            model_for=self.models.for_role,
            backend=self.backend,
            parent_permissions=permissions,
            shared_middleware=[ExcludedToolsMiddleware(frozenset(cfg.harness.excluded_tools))],
        )

    @cached_property
    def agent(self) -> Any:
        sources = self.source_agents()
        return build_agent(
            AgentAssembly(
                cfg=self.agent_config,
                prompts=self.prompts,
                models=self.models,
                tools=self.tools,
                backend=self.backend,
                persistence=self.persistence,
                agent_dir=self.settings.agent_dir,
                middleware=self.agent_middleware(),
                company_name=self.settings.company_name,
                extra_subagents=sources.subagents,
                extra_interrupts=dict(sources.interrupts),
            )
        )

    def prompt_refs(self) -> list[str]:
        p = self.agent_config.prompts
        return [self.prompts.get(n).ref for n in (p.system, p.runtime, p.memory_files)]

    @cached_property
    def consolidation(self) -> Consolidation:
        return Consolidation(
            self.maintenance_db,
            self.personal_memory,
            self.write_guards(),
            self.memory_config,
            self.agent_config,
            self.actions,
            self.backend,
            lambda: self.models,
            self.persistence,
            self.settings.agent_dir,
            self.settings.company_id,
        )

    @cached_property
    def runner(self) -> ChatRunner:
        def on_end(person_id: str, session_id: str) -> None:
            self.consolidation.on_session_end(person_id, session_id)
            self._sync_graph()

        return ChatRunner(
            self.db,
            lambda: self.agent,
            self.memory_files,
            self.settings.company_id,
            prompt_refs=self.prompt_refs,
            on_session_end=on_end,
            after_turn_sync=self._sync_graph,
            on_edited_action=lambda ctx, drafts, decisions: self.edit_learning.learn(ctx, drafts, decisions),
            after_chat=self.consolidation.on_turn,
            empty_reply_retries=self.agent_config.harness.empty_reply_retries,
            empty_reply_nudge=self.agent_config.harness.empty_reply_nudge,
            reply_redactions=self.agent_config.harness.reply_redactions,
        )

    def close(self) -> None:
        if "personal_memory" in self.__dict__:
            self.personal_memory.close()
        if "company_search" in self.__dict__:
            self.company_search.close()
        for client in (self._company_client, self._falkor_client):
            if client is not None and hasattr(client, "close"):
                client.close()
        self._company_client = None
        self._falkor_client = None
        if self._company_server is not None:
            self._company_server.stop()
            self._company_server = None
        for obj in self._keepalive:
            stop = getattr(obj, "stop", None)
            if callable(stop):
                stop()
        if "persistence" in self.__dict__:
            self.persistence.close()
        self.db.close()
        if self.admin_db is not None:
            self.admin_db.close()
