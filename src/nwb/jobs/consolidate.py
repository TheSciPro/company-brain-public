"""Consolidation agent: the memory-keeper subagent, started by a turn counter.

Replaces the Python post-session extractor. The keeper's markdown (``model: background`` and its ``writes:``
list) is the agent. This module does not copy that prompt. A batch smaller than ``min_messages`` is skipped,
so nothing is rewritten. Unattended adds go through; patch and remove are approvals. The run never deletes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends.protocol import BackendProtocol
from langchain.agents.middleware import ModelCallLimitMiddleware
from langgraph.runtime import get_runtime

from nwb.actions.service import ActionService
from nwb.agent.add_only import AddOnlyBackend
from nwb.agent.backends import WritePolicyBackend
from nwb.agent.context import RuntimeContext
from nwb.agent.graph import Models, register_profile
from nwb.agent.middleware.excluded import ExcludedToolsMiddleware
from nwb.agent.persistence import AgentPersistence
from nwb.agent.subagents import SubagentFile, _write_rules, split_markdown
from nwb.conf.models import AgentConfig, MemoryConfig
from nwb.db.repo_directory import DirectoryReader
from nwb.db.repo_jobs import JobRepository
from nwb.db.repo_sessions import MessageRow, SessionRepository, new_id
from nwb.errors import ConfigError
from nwb.identity import Actor
from nwb.memory.consolidate import batch_decision, prepare_transcript
from nwb.memory.guards import WriteGuard
from nwb.memory.store import PersonalMemory
from nwb.obs.log import Hop, dataflow, get_logger, traced
from nwb.ports import SqlDatabase, UnitOfWork

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Step:
    status: str
    pending: int
    added: int = 0
    staged: int = 0
    more: bool = False


@dataclass
class ConsolidationReport:
    session_id: str
    status: str
    pending: int = 0
    added: int = 0
    staged: int = 0


class Consolidation:
    """One deep agent per process, built from the memory-keeper file. The turn counter starts it."""

    JOB = "consolidate"

    def __init__(
        self,
        db: SqlDatabase,
        memory: PersonalMemory,
        guards: Sequence[WriteGuard],
        memory_cfg: MemoryConfig,
        agent_cfg: AgentConfig,
        actions: ActionService,
        backend: BackendProtocol,
        models: Callable[[], Models],
        persistence: AgentPersistence,
        agent_dir: Path,
        company_id: str,
    ) -> None:
        self._db = db
        self._memory = memory
        self._guards = list(guards)
        self._memory_cfg = memory_cfg
        self._agent_cfg = agent_cfg
        self._actions = actions
        self._models = models
        self._persistence = persistence
        self._agent_dir = agent_dir
        self._company = company_id
        self._spec, self._prompt = _load_keeper(agent_dir, memory_cfg.consolidation.subagent)
        self.writes = self._spec.writes
        parent = [
            FilesystemPermission(operations=list(rule.operations), paths=list(rule.paths), mode=rule.mode)
            for rule in agent_cfg.permissions
        ]
        self._rules = _write_rules(self._spec, parent) or []
        self._gate = AddOnlyBackend(backend, self._stage)
        self._guarded = WritePolicyBackend(self._gate, self._rules)
        self._compiled: Any = None

    @traced(step="consolidate.turn", level=logging.DEBUG)
    def on_turn(self, person_id: str, session_id: str) -> ConsolidationReport:
        """Hermes nudge: after ``interval_turns`` new user messages, start the keeper."""
        return self._consider(person_id, session_id, ended=False)

    @traced(step="consolidate.session_end", level=logging.INFO)
    def on_session_end(self, person_id: str, session_id: str) -> ConsolidationReport:
        return self._consider(person_id, session_id, ended=True)

    def sweep(self, limit: int = 20) -> list[ConsolidationReport]:
        with self._db.unit_of_work() as uow:
            pending = SessionRepository(uow).sessions_needing_notes(limit)
        return [self.on_session_end(person_id, session_id) for session_id, person_id in pending]

    def _consider(self, person_id: str, session_id: str, *, ended: bool) -> ConsolidationReport:
        """Consolidate the oldest unprocessed window, then the next, until the rest is not due yet.

        A window is at most ``max_messages`` messages and the watermark is its own last message, so nothing
        after it is marked. When a session ends, every window that is large enough runs before notes are done.
        """
        added = staged = 0
        while True:
            step = self._window(person_id, session_id, ended=ended)
            added, staged = added + step.added, staged + step.staged
            if step.status != "done" or not step.more:
                return ConsolidationReport(session_id, step.status, step.pending, added, staged)

    def _window(self, person_id: str, session_id: str, *, ended: bool) -> _Step:
        cfg = self._memory_cfg.consolidation
        with self._db.unit_of_work(as_person=person_id) as uow:
            sessions = SessionRepository(uow)
            session = sessions.get(session_id)
            if session is None or session.person_id != person_id:
                raise LookupError(f"session {session_id} not found for this user")
            finished = _finished_ids(uow, session_id)
            conversation = sessions.conversation(session_id)
            asof = session.asof or date.today()
        batch = _after(conversation, finished)
        pending = sum(1 for message in batch if message.role == "user")
        decision = batch_decision(pending, cfg.interval_turns, cfg.min_messages, ended=ended)
        if decision != "run":
            if decision == "skip" and ended:
                self._notes(person_id, session_id, "done")
            dataflow(
                Hop.JOB,
                job=self.JOB,
                session=session_id,
                status=decision,
                pending=pending,
                minimum=cfg.min_messages,
            )
            log.info(
                "consolidation %s",
                decision,
                session=session_id,
                pending=pending,
                minimum=cfg.min_messages,
                interval=cfg.interval_turns,
            )
            return _Step(decision, pending)
        window = batch[: cfg.max_messages]  # oldest first; the rest stays eligible for the next window
        more = len(batch) > len(window)
        included = sum(1 for message in window if message.role == "user")
        body, rejected = prepare_transcript(
            [(message.role, message.content) for message in window],
            self._guards,
            self._memory_cfg.facts.chunk_chars,
        )
        last_id = window[-1].id
        if not body.strip():
            # Nothing in this window survived the guards. Mark only these messages, then go on with the rest.
            self._finish_key(session_id, last_id, "skipped", included, 0)
            log.info("consolidation skipped an empty screened window", session=session_id, rejected=rejected)
            if not more and ended:
                self._notes(person_id, session_id, "done")
            return _Step("done" if more else "skipped", pending, more=more)
        run_id = self._claim(session_id, last_id)
        if run_id is None:
            if ended:
                self._notes(person_id, session_id, "done")
            return _Step("skipped", pending)
        if ended:
            self._notes(person_id, session_id, "running")
        try:
            added, staged = self._run(person_id, session_id, asof, body, last_id)
        except Exception as exc:
            log.exception("consolidation %s failed", session_id)
            with self._db.unit_of_work() as uow:
                JobRepository(uow).finish(run_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])
            if ended:
                self._notes(person_id, session_id, "failed")
            return _Step("failed", pending)
        with self._db.unit_of_work() as uow:
            JobRepository(uow).finish(run_id, "done", items_in=included, items_out=added)
        if ended and not more:
            self._notes(person_id, session_id, "done")
        dataflow(
            Hop.JOB,
            job=self.JOB,
            session=session_id,
            status="done",
            pending=included,
            added=added,
            staged=staged,
            rejected=rejected,
            more=more,
        )
        return _Step("done", pending, added, staged, more)

    def _run(self, person_id: str, session_id: str, asof: date, body: str, last_id: str) -> tuple[int, int]:
        when = asof.isoformat()
        names = self._names(person_id, when)
        brief = (
            f"Observed date: {when}\n"
            f"Known names (reuse these spellings): {', '.join(names) or '(none)'}\n\n"
            "You are unattended. Only add. Do not delete. Do not call task.\n\n"
            f"{body}"
        )
        ctx = RuntimeContext(
            self._company,
            person_id,
            session_id,
            new_id("run"),
            when,
            last_id,
            "consolidate",
        )
        self._gate.reset()
        self._graph().invoke(
            {"messages": [{"role": "user", "content": brief}]},
            config={"configurable": {"thread_id": f"consolidate-{session_id}-{last_id}"}},
            context=ctx,
        )
        return self._gate.added, self._gate.staged

    def _names(self, person_id: str, asof: str) -> list[str]:
        with self._db.unit_of_work() as uow:
            directory = DirectoryReader(uow).entity_names()
        actor = Actor(self._company, person_id, datetime.fromisoformat(asof).date())
        graph_names = self._memory.known_entities(actor)
        return _merge(directory, graph_names, self._memory_cfg.facts.known_entity_limit)

    def _graph(self) -> Any:
        if self._compiled is None:
            model = self._models().background
            register_profile(self._agent_cfg, [model])
            excluded = frozenset(self._agent_cfg.harness.excluded_tools) | {"task"}
            limit = ModelCallLimitMiddleware(run_limit=self._spec.model_call_limit, exit_behavior="end")
            self._compiled = create_deep_agent(
                model=model,
                tools=[],
                system_prompt=self._prompt,
                middleware=[ExcludedToolsMiddleware(excluded), limit],
                permissions=self._rules,
                backend=self._guarded,
                context_schema=RuntimeContext,
                checkpointer=self._persistence.checkpointer,
                store=self._persistence.store,
                name=self._spec.name,
            )
        return self._compiled

    def _stage(self, op: str, path: str, before: str, after: str) -> None:
        runtime = get_runtime()
        ctx = runtime.context
        if not isinstance(ctx, RuntimeContext):
            raise PermissionError("consolidation needs a RuntimeContext")
        digest = hashlib.sha256(f"{ctx.user_id}|{op}|{path}|{after}".encode()).hexdigest()[:16]
        self._actions.propose(
            kind_name="memory_change",
            requester=ctx.user_id,
            target=ctx.user_id,
            title=f"Memory {op}: {path}",
            args={
                "op": op,
                "path": path,
                "before": before,
                "after": after,
                "asof": ctx.asof,
                "message": f"Consolidation proposed a {op} of {path}. Nothing was changed.",
            },
            asof=ctx.asof_date,
            proposed_by="consolidation",
            dedupe_key=f"memory:{ctx.user_id}:{digest}",
        )

    def _claim(self, session_id: str, last_id: str) -> str | None:
        with self._db.unit_of_work() as uow:
            jobs = JobRepository(uow)
            # jobs.kind has no "consolidate" value; this pass reuses the post_session kind.
            job_id = jobs.ensure_job(self.JOB, "post_session")
            return jobs.claim(job_id, f"consolidate:{session_id}:{last_id}", "session", session_id)

    def _finish_key(self, session_id: str, last_id: str, status: str, items_in: int, items_out: int) -> None:
        run_id = self._claim(session_id, last_id)
        if run_id is not None:
            with self._db.unit_of_work() as uow:
                JobRepository(uow).finish(run_id, status, items_in=items_in, items_out=items_out)

    def _notes(self, person_id: str, session_id: str, status: str) -> None:
        with self._db.unit_of_work(as_person=person_id) as uow:
            SessionRepository(uow).set_notes_status(session_id, status)


def _load_keeper(agent_dir: Path, name: str) -> tuple[SubagentFile, str]:
    path = agent_dir / "subagents" / f"{name}.md"
    if not path.is_file():
        raise ConfigError(f"consolidation subagent {name!r} is not {path}")
    meta, body = split_markdown(path.read_text(encoding="utf-8"))
    spec = SubagentFile.model_validate(meta)
    if spec.name != name:
        raise ConfigError(f"{path.name}: name {spec.name!r} must match consolidation.subagent")
    if spec.model != "background":
        raise ConfigError(f"{path.name}: consolidation requires model: background")
    return spec, body


def _finished_ids(uow: UnitOfWork, session_id: str) -> set[str]:
    """Last message ids of windows that finished (``done``, or ``skipped`` when nothing survived the guards)."""
    prefix = f"consolidate:{session_id}:"
    rows = uow.fetchall(
        "SELECT idempotency_key FROM job_runs WHERE scope_type = 'session' AND scope_id = :sid "
        "AND status IN ('done', 'skipped') AND idempotency_key LIKE :prefix",
        {"sid": session_id, "prefix": prefix + "%"},
    )
    keys = (str(row["idempotency_key"]) for row in rows)
    return {key[len(prefix) :] for key in keys if key.startswith(prefix)}


def _after(conversation: Sequence[MessageRow], finished: set[str]) -> list[MessageRow]:
    """Messages after the latest finished window, by position in the conversation (not by finish time)."""
    for index in range(len(conversation) - 1, -1, -1):
        if conversation[index].id in finished:
            return list(conversation[index + 1 :])
    return list(conversation)


def _merge(directory: Sequence[str], graph_names: Sequence[str], limit: int) -> list[str]:
    seen: dict[str, None] = {}
    for name in [*directory, *graph_names]:
        text = " ".join(name.split())
        if text:
            seen.setdefault(text, None)
        if len(seen) >= limit:
            break
    return list(seen)
