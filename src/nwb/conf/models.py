"""Pydantic models for ``config/*.yaml``. Frozen and strict: unknown keys are errors, not silent typos."""

from __future__ import annotations

import re
from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nwb.purpose import PURPOSES


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- agent.yaml
class PromptNames(_Frozen):
    system: str = "system"
    runtime: str = "runtime_context"
    bootstrap: str = "onboarding_bootstrap"
    memory_files: str = "memory_files"
    heartbeat: str = "heartbeat"


class HarnessConfig(_Frozen):
    """deepagents harness knobs. Built-ins are ON (ls/read/write/edit/glob/grep, write_todos, task) except these."""

    excluded_tools: frozenset[str] = frozenset({"delete", "execute"})
    general_purpose_subagent: bool = False
    recursion_limit: int = Field(120, ge=4, le=400)
    model_call_limit: int = Field(8, ge=1, le=50)
    tool_call_limit: int = Field(12, ge=1, le=100)
    # A run that ends with no visible reply (no tool call pending, empty content) is asked once more.
    empty_reply_retries: int = Field(1, ge=0, le=2)
    empty_reply_nudge: str = "Reply to the user now, in plain words, based on what you just did."
    # Regex -> replacement applied to every final reply (internal names the model must not show the user).
    reply_redactions: list[tuple[str, str]] = Field(default_factory=list)
    # Tools whose ``stop``+``say`` result ends the main agent's turn as the reply (DirectReplyMiddleware).
    direct_reply_tools: list[str] = Field(default_factory=list)

    @field_validator("excluded_tools")
    @classmethod
    def _delete_always_excluded(cls, v: frozenset[str]) -> frozenset[str]:
        # Safety invariant: the built-in delete tool can never be re-enabled from YAML.
        return v | {"delete"}


class ToolsConfig(_Frozen):
    # Always bound (Memorae: the memory core is always available). Every other tool is bound by a SKILL.md
    # (``metadata.include_tools``) and appears only after the agent reads that skill (progressive disclosure).
    always: tuple[str, ...] = ()


class SkillsConfig(_Frozen):
    sources: tuple[str, ...] = ("/skills/",)


class MemoryFilesConfig(_Frozen):
    # deepagents MemoryMiddleware sources, loaded once per thread (frozen per-session snapshot, Hermes style).
    sources: tuple[str, ...] = ("/org/AGENTS.md", "/memories/USER.md", "/memories/MEMORY.md")


class SummarizationConfig(_Frozen):
    # Tuned for small TPM budgets: summarise early, keep a few recent messages verbatim.
    trigger_tokens: int = Field(5000, ge=500)
    keep_messages: int = Field(6, ge=1)
    trim_tokens: int = Field(3000, ge=500)
    role: Literal["main", "background"] = "background"


class SubagentsConfig(_Frozen):
    dir: str = "subagents"  # under Settings.agent_dir; one markdown file per subagent


class PermissionRule(_Frozen):
    operations: tuple[Literal["read", "write"], ...]
    paths: tuple[str, ...]
    mode: Literal["allow", "deny", "interrupt"]


class PayloadConfig(_Frozen):
    guessed_memories_top_k: int = Field(3, ge=0, le=10)
    guessed_memories_min_score: float = Field(0.25, ge=0.0, le=1.0)
    next_days: int = Field(7, ge=0, le=14)
    show_access_summary: bool = True


class InterruptRule(_Frozen):
    allowed_decisions: tuple[Literal["approve", "edit", "reject"], ...] = ("approve", "reject")


class AgentConfig(_Frozen):
    version: int
    name: str
    timezone: str = "Asia/Calcutta"
    default_asof: date | None = None
    prompts: PromptNames = PromptNames()
    harness: HarnessConfig = HarnessConfig()
    tools: ToolsConfig = ToolsConfig()
    skills: SkillsConfig = SkillsConfig()
    memory_files: MemoryFilesConfig = MemoryFilesConfig()
    summarization: SummarizationConfig = SummarizationConfig()
    subagents: SubagentsConfig = SubagentsConfig()
    permissions: tuple[PermissionRule, ...] = ()
    payload: PayloadConfig = PayloadConfig()
    interrupt_on: dict[str, InterruptRule] = Field(default_factory=dict)

    @field_validator("permissions")
    @classmethod
    def _deny_first(cls, v: tuple[PermissionRule, ...]) -> tuple[PermissionRule, ...]:
        """First match wins and the default is allow: a broad allow before a deny is a leak (smoke test 4b)."""
        seen_allow = False
        for rule in v:
            if rule.mode == "allow" and any(p in {"/**", "/*"} for p in rule.paths):
                seen_allow = True
            if rule.mode == "deny" and seen_allow:
                raise ValueError("a broad allow rule precedes a deny rule; deny rules must come first")
        return v


# ---------------------------------------------------------------- memory.yaml
class EmbedderConfig(_Frozen):
    kind: Literal["fastembed", "hashing"] = "fastembed"
    model: str = "BAAI/bge-small-en-v1.5"
    dim: int = Field(384, ge=8)


class SearchConfig(_Frozen):
    """Stopwords for company keyword search. Dated personal facts are not searched here."""

    min_term_len: int = Field(3, ge=1)
    stopwords: frozenset[str] = frozenset()

    def terms(self, text: str) -> list[str]:
        """Lower-cased content words (company keyword search)."""
        seen: dict[str, None] = {}
        for tok in re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower()):
            if len(tok) >= self.min_term_len and tok not in self.stopwords:
                seen.setdefault(tok, None)
        return list(seen)


class FactsConfig(_Frozen):
    """Graphiti dated-fact recall. Preferences are not a slot: they live in USER.md (C4)."""

    latency_budget_ms: int = Field(4000, ge=1)  # recall, including time spent queued on the shared loop
    io_timeout_ms: int = Field(30_000, ge=1)  # writes and reads; a timed-out call is cancelled
    known_entity_limit: int = Field(40, ge=1, le=200)
    chunk_chars: int = Field(400, ge=50, le=2000)
    slots: dict[str, int] = Field(default_factory=dict)
    preference_kinds: tuple[str, ...] = ("preference",)
    relation_kinds: dict[str, str] = Field(default_factory=dict)
    default_kind: str = "fact"
    extraction_instructions: str = ""


class ConsolidationConfig(_Frozen):
    """Turn counter and batch size for the memory-keeper consolidation agent."""

    interval_turns: int = Field(6, ge=0)  # 0 disables the mid-session counter; session end still runs
    min_messages: int = Field(3, ge=1)  # smaller batches are skipped, not rewritten
    max_messages: int = Field(40, ge=1)
    subagent: str = "memory-keeper"
    sweep_minutes: int = Field(15, ge=1)


class QualityRule(_Frozen):
    """One noise category. Reject when enough pattern hits land in a long-enough text."""

    reason: str
    patterns: tuple[str, ...]
    min_matches: int = Field(1, ge=1)
    min_chars: int = Field(0, ge=0)


class QualityConfig(_Frozen):
    guarded_files: tuple[str, ...] = ("USER.md", "MEMORY.md")
    rejection_suffix: str = "Do not save this and do not retry the same text."
    rules: dict[str, QualityRule] = Field(default_factory=dict)


class EditLearningConfig(_Frozen):
    """A human edit of a drafted action becomes one directive in USER.md."""

    enabled: bool = True
    verb: str = "Prefer"
    verbs: tuple[str, ...] = ("Always", "Never", "Prefer")
    active_status: str = "active"
    superseded_status: str = "superseded"
    prompt: str = "directive_phrase"
    role: Literal["main", "background"] = "background"
    max_chars: int = Field(240, ge=40, le=1500)
    excerpt_chars: int = Field(400, ge=40)
    section: str = "## Directives"
    target_file: str = "USER.md"
    kind_field: str = "kind"
    empty_marker: str = "- (none yet)"
    ignore_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> EditLearningConfig:
        if self.verb not in self.verbs:
            raise ValueError(f"verb {self.verb!r} is not in verbs")
        if self.active_status == self.superseded_status:
            raise ValueError("active and superseded status words must differ")
        return self


class MemoryConfig(_Frozen):
    version: int
    content_max_chars: int = Field(500, ge=50)
    kinds: tuple[str, ...]
    default_sensitivity: str = "highly_confidential"
    authority: int = Field(1, ge=1, le=1)  # chat memories are authority 1, always (AGENTS 6.1)
    secret_patterns: tuple[str, ...] = ()
    embedder: EmbedderConfig = EmbedderConfig()
    search: SearchConfig = SearchConfig()
    facts: FactsConfig = FactsConfig()
    consolidation: ConsolidationConfig = ConsolidationConfig()
    quality: QualityConfig = QualityConfig()
    edit_learning: EditLearningConfig = EditLearningConfig()


# ---------------------------------------------------------------- classification.yaml
class Level(_Frozen):
    code: str
    rank: int
    label: str


class TagRule(_Frozen):
    """One source-system tag. Missing tags use ``unknown_tag`` (fail closed)."""

    level: str
    share: Literal["team", "leadership"] | None = None
    personal: bool = False
    grants: bool = True


class ClassificationConfig(_Frozen):
    version: int
    levels: tuple[Level, ...]
    audit_answers_at_or_above: str = "confidential"
    tags: dict[str, TagRule] = Field(default_factory=dict)
    unknown_tag: TagRule = TagRule(level="highly_confidential", grants=True)

    @model_validator(mode="after")
    def _tags_use_known_levels(self) -> ClassificationConfig:
        codes = {lv.code for lv in self.levels}
        bad = {name: rule.level for name, rule in self.tags.items() if rule.level not in codes}
        if self.unknown_tag.level not in codes:
            bad["unknown_tag"] = self.unknown_tag.level
        if bad:
            raise ValueError(f"classification tags use unknown levels: {bad}")
        return self

    def rank(self, code: str) -> int:
        for lv in self.levels:
            if lv.code == code:
                return lv.rank
        return max(lv.rank for lv in self.levels)  # unknown -> treat as most sensitive (fail closed)

    def resolve_tag(self, tag: str | None) -> TagRule:
        key = (tag or "").strip()
        if not key:
            return self.unknown_tag
        return self.tags.get(key, self.unknown_tag)


# ---------------------------------------------------------------- personas.yaml
class Persona(_Frozen):
    id: str
    label: str
    asof: date | None = None


class PersonasConfig(_Frozen):
    version: int
    personas: tuple[Persona, ...]

    def get(self, person_id: str) -> Persona | None:
        return next((p for p in self.personas if p.id == person_id), None)


# ---------------------------------------------------------------- knowledge.yaml
class ApprovalsConfig(_Frozen):
    self_approval: Literal["up_one_level", "allow"] = "up_one_level"
    max_chain_depth: int = Field(8, ge=1, le=20)


class ResolveConfig(_Frozen):
    """Question -> fact keys discovered in the data (lexical + embedding)."""

    top_k: int = Field(5, ge=1, le=10)  # keys considered
    shown: int = Field(3, ge=1, le=10)  # candidates returned to the model after ranking
    min_score: float = Field(0.12, ge=0.0, le=1.0)
    lexical_weight: float = Field(0.6, ge=0.0, le=1.0)
    # Among keys scoring within this fraction of the best, prefer the one answered at the asker's most specific
    # scope (a client-specific value beats a near-equal general key).
    specific_margin: float = Field(0.65, ge=0.0, le=1.0)
    # Each asker gets a key index built from the facts they may read. This many are kept (least recently used out).
    index_cache: int = Field(64, ge=1)


class ExtractionConfig(_Frozen):
    prompt: str = "fact_extract"
    role: Literal["main", "background"] = "main"
    max_facts_per_doc: int = Field(25, ge=1, le=100)
    max_chars: int = Field(12000, ge=500)
    retries: int = Field(4, ge=0, le=10)
    retry_wait_s: float = Field(20.0, ge=0.0)


class CompanySearchConfig(_Frozen):
    default_limit: int = Field(6, ge=1)
    max_limit: int = Field(12, ge=1)
    snippet_chars: int = Field(240, ge=40)


class KnowledgeConfig(_Frozen):
    version: int = 1
    approvals: ApprovalsConfig = ApprovalsConfig()
    resolve: ResolveConfig = ResolveConfig()
    extraction: ExtractionConfig = ExtractionConfig()
    search: CompanySearchConfig = CompanySearchConfig()


# ---------------------------------------------------------------- sources.yaml (source catalog)
AuthorityName = Literal["chat_or_draft", "minutes_or_decision", "approved_policy", "approved_exception", "contract"]


class AuthorityRule(_Frozen):
    folder: str | None = None
    status_contains: str | None = None
    authority: AuthorityName
    draft: bool = False

    def matches(self, folder: str, status: str) -> bool:
        if self.folder is not None and self.folder != folder:
            return False
        return self.status_contains is None or self.status_contains.lower() in status.lower()


class DocumentSources(_Frozen):
    folders: tuple[str, ...]
    fields: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    authority: tuple[AuthorityRule, ...]
    extract: bool = True

    def field(self, meta: dict[str, object], name: str) -> object | None:
        """First present frontmatter field for a meaning (``id``, ``valid_from``, ``client`` ...)."""
        for f in self.fields.get(name, ()):
            v = meta.get(f)
            if v not in (None, "", []):
                return v
        return None

    def authority_for(self, folder: str, status: str) -> AuthorityRule:
        for rule in self.authority:
            if rule.matches(folder, status):
                return rule
        raise ValueError(f"no authority rule matches folder={folder!r} status={status!r}; add a catch-all rule")


class RecordFact(_Frozen):
    field: str
    unit: str | None = None
    scope: dict[str, str] = Field(default_factory=dict)  # dimension -> record field holding its value
    # The reply sentence for this fact. Placeholders: {value} (with unit), {label}, and each scope dimension
    # ({client} is the client's name). Unset: "The {label} is {value}." The citation is appended.
    say: str | None = None


class RecordSource(_Frozen):
    role: Literal["facts", "approval_matrix", "assignment", "training"] = "facts"
    authority: AuthorityName = "approved_policy"
    valid_from: tuple[str, ...] = ("effective_from", "created_at")
    facts: tuple[RecordFact, ...] = ()
    fields: dict[str, str | tuple[str, ...]] = Field(default_factory=dict)
    any_region: tuple[str, ...] = ()
    inactive_status: tuple[str, ...] = ()

    def f(self, name: str) -> str:
        v = self.fields.get(name)
        if not isinstance(v, str):
            raise KeyError(f"source field {name!r} is not mapped")
        return v

    def fs(self, name: str) -> tuple[str, ...]:
        v = self.fields.get(name, ())
        return (v,) if isinstance(v, str) else tuple(v)


class ConnectorFields(_Frozen):
    """Payload keys for one external source. The mapper is generic; the names live here."""

    external_id: str
    title: str
    body: str
    author: str | None = None
    created_at: str | None = None
    readers: tuple[str, ...] = ()  # comma-separated addresses in each named field become reader principals


class ConnectorDecl(_Frozen):
    """One connected app. A new app is a new entry, not a new Python class."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=10)
    list_action: str = Field(min_length=1)
    items_key: str = "messages"
    max_results: int = Field(50, ge=1, le=200)
    limit_param: str = "max_results"  # the list action's argument for ``max_results``
    # A list action that reads one conversation: its argument name, and the conversations the connected
    # account may read. With ``channel_param`` set and no ``channels``, nothing is read.
    channel_param: str | None = None
    channels: tuple[str, ...] = ()
    # ``user_identities.provider`` that maps this app's user ids (reader refs) to a person. None = email/id only.
    identity_provider: str | None = None
    since_query: str | None = None  # ``after:{unix}`` style; ``{unix}`` is the only placeholder
    fields: ConnectorFields
    writes: tuple[str, ...] = ()  # virtual paths an update may touch; empty = read-only, no update tool
    update_action: str | None = None  # client action used only after human approval
    update_kind: str = "post"
    # How an approved update maps onto ``update_action``: the last segment of the approved path is the
    # target, and it must be in ``post_targets``. Empty = every update is refused.
    post_targets: tuple[str, ...] = ()
    update_target_param: str = "channel"
    update_text_param: str = "text"
    # Response keys that carry the created item's id (checked at the top level, then under ``data``).
    # No id back means the update is reported as failed, not done.
    update_ref_keys: tuple[str, ...] = ("id",)

    @field_validator("writes")
    @classmethod
    def _write_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            if not path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"writes path must be an absolute virtual path without '..': {path!r}")
        return paths

    @model_validator(mode="after")
    def _update_needs_an_action(self) -> ConnectorDecl:
        if self.writes and not self.update_action:
            raise ValueError(f"connector {self.id!r} sets writes but no update_action")
        return self


class DirectoryRoles(_Frozen):
    """How directory groups map to org roles. Group ids are company data, so they live here, not in code."""

    root_unit: str = "ORG"
    leader_groups: tuple[str, ...] = ()
    executive_groups: tuple[str, ...] = ()


class SourcesConfig(_Frozen):
    version: int = 1
    directory: DirectoryRoles = DirectoryRoles()
    documents: DocumentSources
    records: dict[str, RecordSource] = Field(default_factory=dict)
    connectors: tuple[ConnectorDecl, ...] = ()
    # Words a field name uses for its unit ("pct" with "%"); dropped from {label} in a reply sentence.
    unit_words: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    scope_dimensions: tuple[str, ...] = ("client", "country")

    @model_validator(mode="after")
    def _connector_ids_unique(self) -> SourcesConfig:
        ids = [item.id for item in self.connectors]
        dupes = sorted({item for item in ids if ids.count(item) > 1})
        if dupes:
            raise ValueError(f"duplicate connector ids: {dupes}")
        return self

    def by_role(self, role: str) -> dict[str, RecordSource]:
        return {k: v for k, v in self.records.items() if v.role == role}


# ---------------------------------------------------------------- onboarding.yaml / heartbeat.yaml (R3)
OnboardingCategory = Literal["accounts", "training", "policy", "people", "ramp", "access"]


class OnboardingTemplate(_Frozen):
    id: str
    category: OnboardingCategory
    title: str
    due_day: int = Field(ge=0)
    target_ref: str | None = None
    mandatory: bool = True
    verify_rule: str | None = None


class BuddyConfig(_Frozen):
    same_unit: bool = True
    max_grade_gap: int = 2
    min_tenure_days: int = 365


class BriefConfig(_Frozen):
    lookahead_days: int = 7
    max_items: int = 6


class BootstrapConfig(_Frozen):
    top_items: int = 3


class WhenAtom(_Frozen):
    """One ``when`` comparison. ``fact`` is a key the service fills in (never a person or a date)."""

    fact: str
    op: Literal["truthy", "falsy", "eq", "ne", "gt", "gte", "lt", "lte"] = "truthy"
    value: str | int | float | bool | None = None


class WhenClause(_Frozen):
    """Empty matches always. ``all`` and ``any`` both apply when both are set."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    every: tuple[WhenAtom, ...] = Field(default=(), alias="all")
    some: tuple[WhenAtom, ...] = Field(default=(), alias="any")


class OnboardingBeat(_Frozen):
    """One short, skippable step. ``text`` is private guidance; ``example`` is one natural offer line."""

    id: str
    text: str = Field(min_length=1)
    example: str = ""
    when: WhenClause = Field(default_factory=WhenClause)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError(f"beat id must be a lowercase slug, got {value!r}")
        return value


class OnboardingConfig(_Frozen):
    version: int = 1
    new_hire_window_days: int = Field(90, ge=1)
    phases: dict[str, tuple[int, int]] = Field(default_factory=dict)
    buddy: BuddyConfig = BuddyConfig()
    brief: BriefConfig = BriefConfig()
    bootstrap: BootstrapConfig = BootstrapConfig()
    # Only these run purposes get a beat. Heartbeat and cron are not in the list (OpenClaw).
    onboard_purposes: tuple[str, ...] = ("chat",)
    beats: tuple[OnboardingBeat, ...] = ()
    templates: tuple[OnboardingTemplate, ...] = ()

    @field_validator("onboard_purposes")
    @classmethod
    def _known_purposes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(value) - PURPOSES
        if unknown:
            raise ValueError(f"unknown run purpose: {sorted(unknown)}")
        return value

    @model_validator(mode="after")
    def _unique_beats(self) -> OnboardingConfig:
        ids = [beat.id for beat in self.beats]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate onboarding beat id")
        return self

    def phase_for(self, day: int) -> str:
        for name, (lo, hi) in self.phases.items():
            if lo <= day <= hi:
                return name
        return "graduated"


class ActiveHours(_Frozen):
    """Clock window in the agent timezone. ``end`` is exclusive (09:00–19:00 includes 18:59, not 19:00)."""

    start: str = "09:00"
    end: str = "19:00"

    @field_validator("start", "end")
    @classmethod
    def _clock(cls, v: str) -> str:
        try:
            time.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"active hours must be HH:MM, got {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _ordered(self) -> ActiveHours:
        if time.fromisoformat(self.end) <= time.fromisoformat(self.start):
            raise ValueError("active_hours.end must be later than start")
        return self


class WakeCheckConfig(_Frozen):
    """One cheap check in the heartbeat wake gate.

    ``name`` is a key of ``nwb.jobs.heartbeat.PROBES`` (the only list of probe names). Parameters come from here.
    """

    name: str = Field(min_length=1)
    within_days: int = Field(2, ge=0)


class WakeGateConfig(_Frozen):
    """Deterministic checks that run before any model call (Hermes wake gate).

    No signals, or the same signals as a nudge already sent in this dedupe window, means the model is not called.
    """

    skip_when_unchanged: bool = True
    checks: tuple[WakeCheckConfig, ...] = (
        WakeCheckConfig(name="onboarding_due", within_days=2),
        WakeCheckConfig(name="pending_approvals"),
    )


class HeartbeatDelivery(_Frozen):
    """Who hears a heartbeat. ``owner`` is that person's in-app DM; a group is not a valid target.

    The channel stays ``inbox``. A Slack DM is not sent from here.
    """

    to: Literal["owner"] = "owner"
    channel: Literal["inbox"] = "inbox"


class HeartbeatConfig(_Frozen):
    version: int = 1
    enabled: bool = True
    interval_minutes: int = Field(60, ge=1)
    active_hours: ActiveHours = ActiveHours()
    weekdays_only: bool = True
    silence_token: str = "NO_REPLY"  # noqa: S105 - a reply marker, not a secret
    ack_token: str = "HEARTBEAT_OK"  # noqa: S105
    max_chars: int = Field(300, ge=50)
    dedupe_hours: int = Field(20, ge=1)
    use_llm: bool = False
    prompt: str = "heartbeat"
    wake_gate: WakeGateConfig = WakeGateConfig()
    delivery: HeartbeatDelivery = HeartbeatDelivery()


# ---------------------------------------------------------------- actions.yaml (R4)
class ActionKind(_Frozen):
    description: str
    approver: Literal["self", "manager_of_target", "resource_owner", "configured"]
    executor: Literal["inbox", "memory_apply", "apply_ontology"] = "inbox"


class WatchRule(_Frozen):
    enabled: bool = True
    action: str
    min_days_overdue: int = 7
    within_days: int = 30


class WatcherConfig(_Frozen):
    enabled: bool = True
    interval_minutes: int = Field(60, ge=1)
    # Config names (no ``.yaml``) re-read each run. A changed file is reloaded; an unchanged one is left cached.
    reload: tuple[str, ...] = ("actions",)
    rules: dict[str, WatchRule] = Field(default_factory=dict)


class ActionsConfig(_Frozen):
    version: int = 1
    kinds: dict[str, ActionKind] = Field(default_factory=dict)
    watcher: WatcherConfig = WatcherConfig()

    @model_validator(mode="after")
    def _rules_reference_known_kinds(self) -> ActionsConfig:
        unknown = {r.action for r in self.watcher.rules.values()} - set(self.kinds)
        if unknown:
            raise ValueError(f"watcher rules reference unknown action kinds: {sorted(unknown)}")
        return self


# ---------------------------------------------------------------- graph.yaml
class GraphServerConfig(_Frozen):
    """Where GraphRAG-SDK and Graphiti connect. ``url`` (or env FALKORDB_URL) wins; else a local server is
    started from the falkordblite binaries (``nwb.kg.server.LocalFalkorServer``)."""

    local_dir: str = "var/falkordb"
    local_port: int = Field(0, ge=0, le=65535)  # 0 = pick a free port
    persist: bool = True


class RecordingConfig(_Frozen):
    directory: str = "tests/fixtures/llm_calls"
    mode: Literal["live", "record", "replay"] = "record"


class TokenizerConfig(_Frozen):
    """tiktoken files GraphRAG's chunker loads. ``cache_dir`` is filled by ``nwb cache-tokenizers``.

    ``TIKTOKEN_CACHE_DIR`` overrides ``cache_dir`` when it is set (Docker and CI set it at install time).
    """

    cache_dir: str = "var/tiktoken"
    encodings: list[str] = Field(default_factory=lambda: ["cl100k_base"], min_length=1)

    @field_validator("cache_dir")
    @classmethod
    def _cache_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tokenizer.cache_dir is empty")
        return value.strip()

    @field_validator("encodings")
    @classmethod
    def _encodings(cls, values: list[str]) -> list[str]:
        names = [item.strip() for item in values]
        if any(not name for name in names):
            raise ValueError("tokenizer encoding name is empty")
        return names


class EdgeOriginConfig(_Frozen):
    """Turboware: every knowledge edge says whether a source stated it or a job inferred it."""

    extracted: str = "EXTRACTED"
    inferred: str = "INFERRED"
    extracted_confidence: float = Field(1.0, ge=0.0, le=1.0)
    inferred_confidence: float = Field(0.6, ge=0.0, le=1.0)
    inferred_types: tuple[str, ...] = ("SAME_AS",)


class LinkDecl(_Frozen):
    type: str
    to: str
    by: str
    name: str | None = None


class TableDecl(_Frozen):
    """One tabular source. Adding a file is a new entry here, not a code change."""

    path: str
    format: Literal["csv", "json", "jsonl"] = "csv"
    source_name: str  # basename written for the SDK; its stem is the property-name signature
    label: str
    key: str
    name: str
    properties: tuple[str, ...] = ()
    links: tuple[LinkDecl, ...] = ()
    standalone: bool = True


class ProseDecl(_Frozen):
    root: str
    pattern: str = "*.md"
    classification_field: str = "classification"


class GraphLoadConfig(_Frozen):
    classification_field: str = "classification"
    envelope_keys: tuple[str, ...] = ("x_northwind",)
    discover_method: Literal["llm"] = "llm"
    sample_chunks_per_doc: int = Field(1, ge=1, le=8)
    seed: int = 0
    boundaries: str
    tables: tuple[TableDecl, ...] = ()
    prose: tuple[ProseDecl, ...] = ()
    offline_model: str = "offline-kg"

    @model_validator(mode="after")
    def _unique_signatures(self) -> GraphLoadConfig:
        stems = [_signature_stem(item.source_name) for item in self.tables]
        dupes = {stem for stem in stems if stems.count(stem) > 1}
        if dupes:
            raise ValueError(f"table source_name values share a signature: {sorted(dupes)}")
        return self


def _signature_stem(source_name: str) -> str:
    """SDK signature: basename, one extension removed, lowercased. Must be unique across tables."""
    stem = source_name.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem[: stem.rindex(".")]
    return stem.lower()


class FactGraphConfig(_Frozen):
    """Clocks and edge names for company facts stored in each per-level FalkorDB graph.

    Names are identifiers (they become labels and property names). Values in queries stay parameters.
    ``live_statuses`` are eligible to win an as-of read. ``contested_statuses`` are reported, never used.
    ``index_statuses`` are the keys a question can resolve to.
    """

    label: str = "Fact"
    alert_label: str = "Alert"
    about: str = "ABOUT"
    valid_at: str = "valid_at"
    invalid_at: str = "invalid_at"
    recorded_at: str = "recorded_at"
    supersedes: str = "SUPERSEDES"
    overrides: str = "OVERRIDES"
    contradicts: str = "CONTRADICTS"
    live_statuses: tuple[str, ...] = ("current", "superseded")
    contested_statuses: tuple[str, ...] = ("conflict", "draft")
    index_statuses: tuple[str, ...] = ("current", "superseded", "corroborates")
    query_timeout_ms: int = Field(10_000, ge=1000)

    @model_validator(mode="after")
    def _identifiers(self) -> FactGraphConfig:
        ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        names = (
            self.label,
            self.alert_label,
            self.about,
            self.valid_at,
            self.invalid_at,
            self.recorded_at,
            self.supersedes,
            self.overrides,
            self.contradicts,
            *self.live_statuses,
            *self.contested_statuses,
            *self.index_statuses,
        )
        bad = [name for name in names if not ident.match(name)]
        if bad:
            raise ValueError(f"fact graph names must be identifiers, got {bad}")
        return self


class OntologyLifecycleConfig(_Frozen):
    """Approved ontology file, and which discovered changes must wait for a person.

    ``decision_type_pattern`` and ``classification_properties`` are the sensitive
    cases (a new decision type, a classification change). They are config, not
    company names. ``reviewer_role`` picks the directory group in ``sources.yaml``.
    """

    file: str = "config/ontology.json"
    pending_file: str = "var/ontology/pending.json"
    state_file: str = "var/ontology/state.json"
    reviewer_role: Literal["executive", "leader"] = "executive"
    decision_type_pattern: str = r"(?i)decis"
    classification_properties: tuple[str, ...] = ("classification", "cls")
    action_kind: str = "review_ontology"


class CompanyGraphConfig(_Frozen):
    graph_name: str = "{company}_kb_{level}"  # one FalkorDB graph per company per access level
    ontology: OntologyLifecycleConfig = OntologyLifecycleConfig()
    ner: Literal["llm", "gliner"] = "llm"  # gliner needs torch; llm keeps the install small
    max_concurrency: int = Field(1, ge=1, le=8)  # free-tier TPM: one chunk at a time
    chunk_chars: int = Field(1500, ge=200)
    min_evidence: int = Field(1, ge=0)  # passages at or above the floor needed before an answer is attempted
    evidence_floor: float = Field(0.45, ge=0.0, le=1.0)  # top score required; below this, return no chunks
    retrieve_limit: int = Field(6, ge=1, le=20)
    query_timeout_ms: int = Field(120_000, ge=1000)  # index builds and retrieve exceed the 10s SDK default
    enable_cypher: bool = True
    edge_origin: EdgeOriginConfig = EdgeOriginConfig()
    facts: FactGraphConfig = FactGraphConfig()
    load: GraphLoadConfig | None = None


class PersonalGraphConfig(_Frozen):
    group_id: str = "{company}__{user}"  # Graphiti group_id == one FalkorDB graph per user
    semaphore_limit: int = Field(1, ge=1, le=20)
    search_limit: int = Field(8, ge=1, le=30)
    entity_types: dict[str, str] = Field(default_factory=dict)  # name -> description; preferences are not a type


class GraphConfig(_Frozen):
    version: int
    server: GraphServerConfig = GraphServerConfig()
    recording: RecordingConfig = RecordingConfig()
    tokenizer: TokenizerConfig = TokenizerConfig()
    company: CompanyGraphConfig = CompanyGraphConfig()
    personal: PersonalGraphConfig = PersonalGraphConfig()

    def company_graph(self, company: str, level: str, *, prefix: str = "") -> str:
        """``{prefix}{company}_kb_{level}``. ``prefix`` keeps a cloud run off shared graph names."""
        body = self.company.graph_name.format(company=_graph_safe(company), level=_graph_safe(level))
        safe_prefix = _graph_safe(prefix) if prefix else ""
        if safe_prefix and not safe_prefix.endswith("_"):
            safe_prefix = f"{safe_prefix}_"
        return f"{safe_prefix}{body}"

    def user_group(self, company: str, user: str) -> str:
        return _graph_safe(self.personal.group_id.format(company=company.lower(), user=user.lower()))


def _graph_safe(raw: str) -> str:
    """Graph names / Graphiti group ids allow ``[A-Za-z0-9_-]``."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", raw).lower()


# ---------------------------------------------------------------- resolution.yaml
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ResolutionScores(_Frozen):
    """Scores recovered from the deleted name resolver. None of them merge a person."""

    exact_link: float = Field(1.0, ge=0.0, le=1.0)
    normalised_suffix: float = Field(0.95, ge=0.0, le=1.0)
    initial_surname: float = Field(0.9, ge=0.0, le=1.0)
    reordered_words: float = Field(0.85, ge=0.0, le=1.0)
    shared_name: float = Field(0.5, ge=0.0, le=1.0)
    ambiguous_initial: float = Field(0.4, ge=0.0, le=1.0)

    def value(self, key: str) -> float:
        return float(getattr(self, key))


class ResolutionReasons(_Frozen):
    """Substrings of the text ``finalize()`` writes on a probable duplicate."""

    normalised_suffix: str
    initial_surname: str
    reordered_words: str
    shared_name: str


class NameNorm(_Frozen):
    """How a display name is folded. The old resolver's ``normalise_name``, as data."""

    ascii_fold: bool = True
    drop_parenthetical: bool = True
    strip_pattern: str
    initial_pattern: str

    @model_validator(mode="after")
    def _patterns_compile(self) -> NameNorm:
        re.compile(self.strip_pattern)
        re.compile(self.initial_pattern)
        return self


class IdentityField(_Frozen):
    """One identity column. ``property`` is the graph key, or the suffix after a source signature."""

    field: str
    property: str
    normalise: Literal["exact", "lower", "name"] = "exact"
    conflict: bool = True  # a difference blocks a name suggestion; a shared value still links

    @model_validator(mode="after")
    def _names(self) -> IdentityField:
        if not _IDENT.match(self.field) or not _IDENT.match(self.property):
            raise ValueError(f"identity field names must be identifiers, got {self.field!r} / {self.property!r}")
        return self


class IdentityRule(_Frozen):
    """A label that is linked by id or email and never name-merged."""

    label: str
    never_merge: Literal[True] = True
    link_by: tuple[IdentityField, ...]
    keep_apart: tuple[IdentityField, ...] = ()

    @model_validator(mode="after")
    def _label(self) -> IdentityRule:
        if not _IDENT.match(self.label):
            raise ValueError(f"identity label must be an identifier, got {self.label!r}")
        if not self.link_by:
            raise ValueError(f"{self.label} needs at least one link field")
        return self


class LinkEdgeConfig(_Frozen):
    type: str = "IDENTIFIES"
    origin: str = "INFERRED"

    @model_validator(mode="after")
    def _type(self) -> LinkEdgeConfig:
        if not _IDENT.match(self.type):
            raise ValueError(f"link type must be an identifier, got {self.type!r}")
        return self


class ResolutionInbox(_Frozen):
    kind: str
    proposed_by: str = "resolution"


class ResolutionConfig(_Frozen):
    version: int
    suggestion_threshold: float = Field(0.9, ge=0.0, le=1.0)
    scores: ResolutionScores
    reasons: ResolutionReasons
    name: NameNorm
    identities: tuple[IdentityRule, ...]
    link: LinkEdgeConfig = LinkEdgeConfig()
    inbox: ResolutionInbox

    @model_validator(mode="after")
    def _unique_labels(self) -> ResolutionConfig:
        labels = [rule.label for rule in self.identities]
        dupes = {label for label in labels if labels.count(label) > 1}
        if dupes:
            raise ValueError(f"identity rules repeat labels: {sorted(dupes)}")
        return self

    def rule_for(self, label: str) -> IdentityRule | None:
        for rule in self.identities:
            if rule.label == label:
                return rule
        return None
