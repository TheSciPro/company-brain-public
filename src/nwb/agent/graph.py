"""The agent: ONE ``create_deep_agent`` call, assembled from config and markdown assets.

What deepagents provides (switched on): the virtual filesystem tools over our ``CompositeBackend``, ``write_todos``,
``task`` with our markdown subagents, skills with progressive tool disclosure (``SkillsMiddleware``), memory files
(``MemoryMiddleware`` over AGENTS.md / USER.md / MEMORY.md), summarisation, HITL (``interrupt_on``), filesystem
permissions, ``context_schema`` and the checkpointer + store.

What we add (``nwb.agent.middleware``): the per-run ``<runtime>`` block, the first-contact bootstrap and tool-call
receipts. The turn counter that starts memory consolidation lives in ``nwb.jobs.consolidate``, not in the prompt.
Plus per-run limits. Provider fallback lives on the chat model (``with_fallbacks``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    MemoryMiddleware,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import (
    AgentMiddleware,
    InterruptOnConfig,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from nwb.agent.backends import MEMORIES, SKILLS
from nwb.agent.context import RuntimeContext
from nwb.agent.middleware.approved_call import ApprovedCallMiddleware
from nwb.agent.middleware.direct_reply import DirectReplyMiddleware
from nwb.agent.middleware.excluded import ExcludedToolsMiddleware
from nwb.agent.persistence import AgentPersistence
from nwb.agent.skills import skill_tool_names
from nwb.agent.subagents import build_subagents
from nwb.conf.models import AgentConfig, InterruptRule
from nwb.errors import ConfigError
from nwb.llm.fallback import model_steps
from nwb.obs.log import get_logger, traced
from nwb.prompts import PromptLibrary

log = get_logger(__name__)
ModelRole = Literal["main", "background"]


@dataclass(frozen=True)
class Models:
    main: BaseChatModel
    background: BaseChatModel

    def for_role(self, role: ModelRole) -> BaseChatModel:
        return self.main if role == "main" else self.background


@dataclass(frozen=True)
class AgentAssembly:
    """Everything the graph is made of. Built by the container; no I/O here beyond reading ``agent/``."""

    cfg: AgentConfig
    prompts: PromptLibrary
    models: Models
    tools: Mapping[str, BaseTool]  # every domain tool, by name
    backend: BackendProtocol
    persistence: AgentPersistence
    agent_dir: Path
    middleware: Sequence[AgentMiddleware[Any, Any, Any]]  # ours: runtime context, bootstrap, audit
    company_name: str
    # One subagent per configured source (B3). Built by the container; empty in tests that assemble by hand.
    extra_subagents: tuple[SubAgent, ...] = ()
    extra_interrupts: Mapping[str, InterruptRule] = field(default_factory=dict)


def provider_key(model: BaseChatModel) -> str:
    """deepagents resolves a ``HarnessProfile`` by the model's LangSmith provider (e.g. ``nvidia``)."""
    params: dict[str, Any] = dict(model._get_ls_params())
    return str(params.get("ls_provider") or type(model).__name__.lower())


def register_profile(cfg: AgentConfig, models: Sequence[BaseChatModel]) -> None:
    profile = HarnessProfile(
        excluded_tools=frozenset(cfg.harness.excluded_tools),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=cfg.harness.general_purpose_subagent),
    )
    for key in {provider_key(m) for m in models}:
        register_harness_profile(key, profile)


def split_tools(
    cfg: AgentConfig, tools: Mapping[str, BaseTool], skill_bound: set[str]
) -> tuple[list[BaseTool], list[BaseTool]]:
    """(always bound, skill bound). Every tool must be reachable one way or the other: no dead tools."""
    always = [tools[n] for n in cfg.tools.always if n in tools]
    missing = (set(cfg.tools.always) | skill_bound) - set(tools)
    if missing:
        raise ConfigError(f"tools named in config/skills but not registered: {sorted(missing)}")
    unreachable = set(tools) - set(cfg.tools.always) - skill_bound
    if unreachable:
        raise ConfigError(f"tools no skill binds and not always-on: {sorted(unreachable)}")
    return always, [tools[n] for n in sorted(skill_bound - set(cfg.tools.always))]


@traced(step="agent.build", level=logging.INFO)
def build_agent(a: AgentAssembly) -> CompiledStateGraph[Any, Any, Any, Any]:
    cfg = a.cfg
    skill_bound = skill_tool_names(a.agent_dir / "skills")
    always, gated = split_tools(cfg, a.tools, skill_bound)
    if unknown := set(cfg.harness.direct_reply_tools) - set(a.tools):
        raise ConfigError(f"direct_reply_tools not registered: {sorted(unknown)}")
    excluded = frozenset(cfg.harness.excluded_tools)
    permissions = [
        FilesystemPermission(operations=list(p.operations), paths=list(p.paths), mode=p.mode) for p in cfg.permissions
    ]
    subagents = [
        *build_subagents(
            a.agent_dir / cfg.subagents.dir,
            a.tools,
            a.models.for_role,
            [ExcludedToolsMiddleware(excluded)],
            backend=a.backend,
            parent_permissions=permissions,
        ),
        *a.extra_subagents,
    ]
    names = [spec["name"] for spec in subagents]
    if len(names) != len(set(names)):
        raise ConfigError(f"duplicate subagent names: {sorted(n for n in names if names.count(n) > 1)}")
    register_profile(cfg, [*model_steps(a.models.main), *model_steps(a.models.background)])

    limits: list[AgentMiddleware[Any, Any, Any]] = [
        ModelCallLimitMiddleware(run_limit=cfg.harness.model_call_limit, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=cfg.harness.tool_call_limit, exit_behavior="continue"),
    ]
    s = cfg.summarization
    builtins: list[AgentMiddleware[Any, Any, Any]] = [
        TodoListMiddleware(),  # write_todos (deepagents 0.7 no longer adds it by default)
        SkillsMiddleware(
            backend=a.backend,
            sources=list(cfg.skills.sources),
            tools=gated,
            system_prompt=a.prompts.render("skills_system").text,
        ),
        MemoryMiddleware(
            backend=a.backend,
            sources=list(cfg.memory_files.sources),
            system_prompt=a.prompts.render(cfg.prompts.memory_files).text,
        ),
        SummarizationMiddleware(
            a.models.for_role(s.role),
            backend=a.backend,
            trigger=("tokens", s.trigger_tokens),
            keep=("messages", s.keep_messages),
            trim_tokens_to_summarize=s.trim_tokens,
        ),
    ]
    overlap = set(cfg.interrupt_on) & set(a.extra_interrupts)
    if overlap:
        raise ConfigError(f"source update tools collide with interrupt_on: {sorted(overlap)}")
    interrupt_on: dict[str, bool | InterruptOnConfig] = {
        name: InterruptOnConfig(allowed_decisions=list(rule.allowed_decisions))
        for name, rule in {**cfg.interrupt_on, **a.extra_interrupts}.items()
    }
    system = a.prompts.render(cfg.prompts.system, company_name=a.company_name, agent_name=cfg.name).text
    graph = create_deep_agent(
        model=a.models.main,
        tools=always,
        system_prompt=system,
        middleware=[
            ExcludedToolsMiddleware(excluded),
            *builtins,
            # After skill disclosure, before the audit wrapper, so a reviewed edit is
            # what gets logged and what runs. HITL (appended by deepagents) still
            # substitutes again from the same resume payload.
            ApprovedCallMiddleware(dict(a.tools), frozenset(interrupt_on)),
            *a.middleware,
            DirectReplyMiddleware(frozenset(cfg.harness.direct_reply_tools)),
            *limits,
        ],
        subagents=subagents,
        permissions=permissions,
        backend=a.backend,
        interrupt_on=interrupt_on,
        context_schema=RuntimeContext,
        checkpointer=a.persistence.checkpointer,
        store=a.persistence.store,
        name=cfg.name,
    )
    log.info(
        "agent built: always=%s skill_bound=%s subagents=%s memory=%s skills=%s",
        [t.name for t in always],
        [t.name for t in gated],
        [sa["name"] for sa in subagents],
        MEMORIES,
        SKILLS,
    )
    return graph.with_config(recursion_limit=cfg.harness.recursion_limit)
