"""ChatRunner: the run lifecycle around the one agent graph.

  user message -> SQL (messages, agent_runs) -> graph.stream(thread_id = session_id, context = RuntimeContext)
  -> ChatEvent stream (tokens, tool start/end, receipts, interrupts) -> SQL (assistant message, run status)

One conversation is one LangGraph thread: the checkpointer holds the history, the summariser keeps it inside the
token budget, and an interrupted run (HITL) resumes on the same thread. Scheduled runs (heartbeat) use the same graph
with ``purpose="heartbeat"`` on the user's own heartbeat thread and stay silent on ``NO_REPLY``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from datetime import date
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command

from nwb.agent.context import Purpose, RuntimeContext
from nwb.agent.events import ChatEvent, TurnResult
from nwb.agent.memory_files import MemoryFiles
from nwb.agent.middleware.direct_reply import direct_reply_of
from nwb.db.repo_sessions import SessionRepository, new_id
from nwb.errors import ConflictError, NotFoundError
from nwb.memory.edit_learning import DirectiveRejectedError
from nwb.memory.phrasing import RecordingMissError
from nwb.obs.log import Hop, dataflow, get_logger, same_trace
from nwb.onboarding.marker import ReplyScrubber, visible_reply
from nwb.ports import SqlDatabase

log = get_logger(__name__)
GraphProvider = Callable[[], Any]
SessionEndHook = Callable[[str, str], None]
AfterTurn = Callable[[str, str], None]
SyncHook = Callable[[], None]
EditedActionHook = Callable[
    [RuntimeContext, Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
    Sequence[Mapping[str, Any]] | None,
]
RECEIPT_EVENTS = frozenset({"ADD", "UPDATE", "NOOP", "REJECTED"})
_LIMIT_HIT = re.compile(r"Model call limits exceeded|run limit \(\d+/\d+\)", re.IGNORECASE)
ABSTAIN_UNCONFIRMED = (
    "I could not confirm this from the company record in this turn. "
    "This is not on record for you from what I could check. "
    "Ask your manager or the document owner."
)


def drop_tool_cites(text: str, tools: set[str]) -> str:
    """Remove ``[tool_name]`` "citations": a tool's name is not a source, and the chip reads as tool noise."""
    if not tools:
        return text
    names = "|".join(re.escape(t) for t in sorted(tools))
    # "week [onboarding_brief] ." loses the space before the full stop too.
    return re.sub(rf"[ \t]*[\[【](?:{names})[\]】]([ \t]+(?=[.,;:]))?", "", text)


def redact(text: str, rules: Sequence[tuple[re.Pattern[str], str]], run_id: str = "") -> str:
    """Replace internal names (memory file paths) in a final reply; log each rule that fired."""
    for rx, repl in rules:
        text, hits = rx.subn(repl, text)
        if hits:
            log.info("reply redacted", run=run_id, rule=rx.pattern, hits=hits)
    return text


def public_answer(text: str) -> str:
    """What the user may see. A model-call-limit message becomes an abstention, never the raw error."""
    if text and _LIMIT_HIT.search(text):
        return ABSTAIN_UNCONFIRMED
    return text


# Fixed phrases the limit regex can grow into. A streamed suffix of either is held, not sent.
_LIMIT_PHRASES = ("model call limits exceeded", "run limit (")
_PARTIAL_RUN = re.compile(r"run limit \(\d*(?:/\d*)?$", re.IGNORECASE)


def _unsafe_tail(text: str) -> int:
    """How many trailing characters might still grow into the model-call-limit message."""
    low = text.lower()
    hold = 0
    for phrase in _LIMIT_PHRASES:
        for size in range(1, len(phrase)):
            if low.endswith(phrase[:size]):
                hold = max(hold, size)
    partial = _PARTIAL_RUN.search(low)
    if partial is not None:
        hold = max(hold, len(text) - partial.start())
    return hold


class _TokenGate:
    """Token text a live client may render. The limit phrase is never one of those tokens."""

    def __init__(self) -> None:
        self.buffer = ""
        self._sent = 0
        self.blocked = False

    def push(self, piece: str) -> str:
        if self.blocked or not piece:
            return ""
        self.buffer += piece
        if _LIMIT_HIT.search(self.buffer):
            self.blocked = True
            self.buffer = ABSTAIN_UNCONFIRMED
            self._sent = len(self.buffer)
            return ""
        safe = len(self.buffer) - _unsafe_tail(self.buffer)
        if safe <= self._sent:
            return ""
        out = self.buffer[self._sent : safe]
        self._sent = safe
        return out

    def flush(self) -> str:
        """Send a held tail once the message is finished and it was not the limit phrase."""
        if self.blocked or self._sent >= len(self.buffer):
            return ""
        out = self.buffer[self._sent :]
        self._sent = len(self.buffer)
        return out

    def drop_message(self) -> None:
        """A tool call ends this assistant message. Keep a block for the rest of the run."""
        if self.blocked:
            return
        self.buffer = ""
        self._sent = 0


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") if isinstance(p, dict) and p.get("type", "text") == "text" else "" for p in content
        )
    return str(content)


class ChatRunner:
    def __init__(
        self,
        db: SqlDatabase,
        graph: GraphProvider,
        memory_files: MemoryFiles,
        company_id: str,
        prompt_refs: Callable[[], list[str]] | None = None,
        on_session_end: SessionEndHook | None = None,
        after_turn_sync: SyncHook | None = None,
        on_edited_action: EditedActionHook | None = None,
        after_chat: AfterTurn | None = None,
        empty_reply_retries: int = 0,
        empty_reply_nudge: str = "",
        reply_redactions: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._db, self._graph, self._files, self._company = db, graph, memory_files, company_id
        self._empty_retries, self._empty_nudge = empty_reply_retries, empty_reply_nudge
        self._redactions = [(re.compile(p, re.IGNORECASE), repl) for p, repl in reply_redactions]
        self._refs = prompt_refs or (lambda: [])
        self._on_end, self._sync, self._after_chat = on_session_end, after_turn_sync, after_chat
        self._on_edit = on_edited_action
        self._pending_drafts: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ sessions
    def start_session(self, person_id: str, asof: date, session_id: str | None = None) -> str:
        """A session is a thread. USER.md / MEMORY.md are created from templates on first contact."""
        sid = session_id or new_id("s")
        created = self._files.ensure(self._company, person_id, asof)
        with self._db.unit_of_work(as_person=person_id, asof=asof) as uow:
            repo = SessionRepository(uow)
            if repo.get(sid) is None:
                repo.create(sid, person_id, asof, "")
        dataflow(Hop.SESSION, event="start", session=sid, person=person_id, asof=asof.isoformat(), seeded=created)
        return sid

    def end_session(self, person_id: str, session_id: str) -> None:
        with self._db.unit_of_work(as_person=person_id) as uow:
            repo = SessionRepository(uow)
            row = repo.get(session_id)
            if row is None or row.person_id != person_id:
                raise NotFoundError(f"session {session_id} not found for this user")
            repo.end(session_id)
        dataflow(Hop.SESSION, event="end", session=session_id, person=person_id)
        if self._on_end is not None:
            self._on_end(person_id, session_id)

    # ------------------------------------------------------------------ turns
    def turn(self, person_id: str, session_id: str, text: str) -> TurnResult:
        return collect(session_id, list(self.stream(person_id, session_id, text)))

    def stream(self, person_id: str, session_id: str, text: str, purpose: Purpose = "chat") -> Iterator[ChatEvent]:
        # Stay on the request's trace. ``same_trace`` opens one only when the caller has none.
        with same_trace():
            ctx = self._open_run(person_id, session_id, text, purpose)
            yield ChatEvent(type="run_started", run_id=ctx.run_id, data={"session_id": session_id, "asof": ctx.asof})
            yield from self._drive(ctx, {"messages": [{"role": "user", "content": text}]})

    def scheduled(self, person_id: str, asof: date, purpose: Purpose, text: str) -> TurnResult:
        """A turn with no user message (heartbeat, consolidation) on the user's ``<purpose>:<user>`` thread."""
        sid = self.start_session(person_id, asof, session_id=f"{purpose}-{person_id}")
        return collect(sid, list(self.stream(person_id, sid, text, purpose)))

    def resume(self, person_id: str, run_id: str, decisions: list[dict[str, Any]]) -> Iterator[ChatEvent]:
        """Resume an interrupted run (HITL) on its session thread with the human's decisions.

        An edit is the batched LangGraph payload ``{"type": "edit", "edited_action":
        {"name", "args"}}``. Those args are the ones the tool runs.
        """
        with same_trace():
            with self._db.unit_of_work(as_person=person_id) as uow:
                repo = SessionRepository(uow)
                found = repo.run_thread(run_id)
                if found is None:
                    raise NotFoundError(f"run {run_id} not found for this user")
                _, session_id = found
                session = repo.get(session_id)
                trigger = uow.fetchone("SELECT trigger_message_id FROM agent_runs WHERE id = :id", {"id": run_id})
            if session is None or trigger is None or session.person_id != person_id:
                raise NotFoundError(f"run {run_id} has no session for this user")
            ctx = RuntimeContext(
                self._company,
                person_id,
                session_id,
                run_id,
                (session.asof or date.today()).isoformat(),
                str(trigger["trigger_message_id"]),
            )
            drafts = self._pending_drafts.get(session_id) or self._drafts_from_state(session_id)
            final_status: str | None = None
            for event in self._drive(ctx, Command(resume={"decisions": decisions})):
                if event.type == "final":
                    final_status = str(event.data.get("status") or "")
                yield event
            self._pending_drafts.pop(session_id, None)
            if self._on_edit is not None and final_status == "done" and _has_edit(decisions):
                try:
                    learned = self._on_edit(ctx, drafts, decisions)
                except DirectiveRejectedError as exc:
                    learned = exc.receipts or [{"event": "REJECTED", "reason": str(exc)}]
                except RecordingMissError:
                    raise
                except Exception:
                    log.exception("learning from an edited action failed", run=ctx.run_id, session=session_id)
                    learned = None
                for row in _receipt_rows(learned):
                    yield ChatEvent(type="receipt", run_id=ctx.run_id, data=dict(row))

    def _open_run(self, person_id: str, session_id: str, text: str, purpose: Purpose) -> RuntimeContext:
        run_id = new_id("run")
        role = "user" if purpose == "chat" else "system"
        with self._db.unit_of_work(as_person=person_id) as uow:
            repo = SessionRepository(uow)
            session = repo.get(session_id)
            if session is None or session.person_id != person_id:
                raise NotFoundError(f"session {session_id} not found for this user")
            if session.ended:
                raise ConflictError(f"session {session_id} has ended; start a new one")
            msg_id = repo.add_message(session_id, person_id, role, text)
            repo.bump_turn(session_id)
            repo.start_run(run_id, session_id, person_id, msg_id, thread_id=session_id)
        asof = (session.asof or date.today()).isoformat()
        dataflow(Hop.AGENT_RUN, event="start", run=run_id, session=session_id, person=person_id, purpose=purpose)
        return RuntimeContext(self._company, person_id, session_id, run_id, asof, msg_id, purpose)

    def _drive(self, ctx: RuntimeContext, inp: Any) -> Iterator[ChatEvent]:
        t0 = time.perf_counter()
        config = {"configurable": {"thread_id": ctx.session_id}}
        gate = _TokenGate()
        scrubber = ReplyScrubber()
        answer, interrupt = "", None
        try:
            for attempt in range(1 + self._empty_retries):
                if attempt:
                    # The model ended without a reply (seen with reasoning models). Ask once more, same thread.
                    log.warning("empty reply; asking the model again", run=ctx.run_id, attempt=attempt)
                    inp = {"messages": [{"role": "user", "content": self._empty_nudge}]}
                    gate, scrubber = _TokenGate(), ReplyScrubber()
                answer, interrupt = yield from self._stream_once(ctx, inp, config, gate, scrubber)
                if answer or interrupt:
                    break
            status = "interrupted" if interrupt else "done"
            ms = int((time.perf_counter() - t0) * 1000)
            self._close(ctx, answer, status, None, ms)
            yield ChatEvent(type="final", run_id=ctx.run_id, data={"answer": answer, "status": status, "ms": ms})
        except Exception as exc:  # the run fails, the service does not: record it and tell the client
            log.exception("agent run %s failed", ctx.run_id, run=ctx.run_id, session=ctx.session_id)
            ms = int((time.perf_counter() - t0) * 1000)
            self._close(ctx, "", "failed", f"{type(exc).__name__}: {exc}"[:500], ms)
            yield ChatEvent(
                type="error", run_id=ctx.run_id, data={"error": type(exc).__name__, "message": friendly(exc)}
            )

    def _stream_once(
        self, ctx: RuntimeContext, inp: Any, config: dict[str, Any], gate: _TokenGate, scrubber: ReplyScrubber
    ) -> Generator[ChatEvent, None, tuple[str, dict[str, Any] | None]]:
        """One pass of the graph. Yields the client events; returns the visible answer and any interrupt."""
        answer, interrupt, direct, called = "", None, "", set[str]()
        stream = self._graph().stream(
            inp, config=config, context=ctx, stream_mode=["messages", "updates"], subgraphs=True
        )
        for ns, mode, chunk in stream:
            root = not ns
            if mode == "messages":
                msg, meta = chunk
                speaks = root and meta.get("lc_source") != "summarization" and isinstance(msg, AIMessageChunk)
                # Onboarding markers are scrubbed first, then the limit-phrase gate decides what streams.
                if (
                    speaks
                    and (piece := text_of(msg.content))
                    and (shown := scrubber.push(piece))
                    and (delta := gate.push(shown))
                ):
                    yield ChatEvent(type="token", run_id=ctx.run_id, data={"text": delta})
                continue
            for node, update in (chunk or {}).items():
                if node == "__interrupt__":
                    values = [getattr(i, "value", i) for i in update]
                    interrupt = {"interrupts": values}
                    drafts = [req for value in values if isinstance(value, dict) for req in action_requests(value)]
                    if drafts:
                        self._pending_drafts[ctx.session_id] = drafts
                    yield ChatEvent(type="interrupt", run_id=ctx.run_id, data=interrupt)
                    continue
                msgs = update.get("messages", []) if isinstance(update, dict) else []
                for m in msgs if isinstance(msgs, list) else []:
                    if isinstance(m, AIMessage) and m.tool_calls:
                        if root:
                            gate.drop_message()
                            scrubber = ReplyScrubber()
                        for tc in m.tool_calls:
                            called.add(tc["name"])
                            data = {"tool": tc["name"], "args": tc.get("args", {}), "subagent": not root}
                            yield ChatEvent(type="tool_start", run_id=ctx.run_id, data=data)
                    elif isinstance(m, AIMessage) and root:
                        raw = text_of(m.content)
                        answer = public_answer(visible_reply(raw) if raw else gate.buffer)
                    elif isinstance(m, ToolMessage):
                        if root:
                            direct = direct_reply_of(m.artifact) or direct
                        yield from tool_end_events(ctx.run_id, m, subagent=not root)
        if (rest := scrubber.finish()) and (delta := gate.push(rest)):
            yield ChatEvent(type="token", run_id=ctx.run_id, data={"text": delta})
        if tail := gate.flush():
            yield ChatEvent(type="token", run_id=ctx.run_id, data={"text": tail})
        answer = public_answer(visible_reply(answer) if answer else gate.buffer)
        if not answer and not interrupt and direct:
            # A direct_reply tool ended the turn: its ``say`` sentence is the reply.
            answer = public_answer(direct)
            yield ChatEvent(type="token", run_id=ctx.run_id, data={"text": answer})
        return redact(drop_tool_cites(answer, called), self._redactions, ctx.run_id), interrupt

    def _close(self, ctx: RuntimeContext, answer: str, status: str, error: str | None, ms: int | None = None) -> None:
        with self._db.unit_of_work(as_person=ctx.user_id) as uow:
            repo = SessionRepository(uow)
            if answer:
                repo.add_message(ctx.session_id, ctx.user_id, "assistant", answer)
            repo.finish_run(ctx.run_id, status, prompt_refs=self._refs(), error=error)
        dataflow(Hop.AGENT_RUN, event="end", run=ctx.run_id, status=status, error=error, ms=ms, content=answer)
        if self._sync is not None:
            try:
                self._sync()
            except Exception:  # the graph is a projection: the outbox retries later
                log.exception("post-turn graph sync failed; outbox keeps the rows")
        if ctx.purpose == "chat" and status == "done" and self._after_chat is not None:
            try:
                self._after_chat(ctx.user_id, ctx.session_id)
            except Exception:
                log.exception("consolidation after a chat turn failed", session=ctx.session_id)

    def _drafts_from_state(self, session_id: str) -> list[dict[str, Any]]:
        """Original action requests still waiting on the thread, for a resume after a process restart."""
        try:
            snap = self._graph().get_state({"configurable": {"thread_id": session_id}})
        except Exception:
            log.exception("could not read the pending approval draft", session=session_id)
            return []
        drafts: list[dict[str, Any]] = []
        for intr in getattr(snap, "interrupts", ()) or ():
            value = getattr(intr, "value", None)
            if isinstance(value, dict):
                drafts.extend(action_requests(value))
        return drafts


def action_requests(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = value.get("action_requests")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _has_edit(decisions: Sequence[Mapping[str, Any]]) -> bool:
    return any(d.get("type") == "edit" for d in decisions)


def _receipt_rows(learned: object) -> list[Mapping[str, Any]]:
    if isinstance(learned, (str, bytes)) or not isinstance(learned, Sequence):
        return []
    return [row for row in learned if isinstance(row, Mapping)]


def tool_end_events(run_id: str, m: ToolMessage, *, subagent: bool = False) -> Iterator[ChatEvent]:
    text = text_of(m.content)
    yield ChatEvent(
        type="tool_end",
        run_id=run_id,
        data={
            "tool": m.name,
            "status": m.status,
            "preview": text[:300],
            "subagent": subagent,
            **sources_of(m.artifact),
        },
    )
    art = m.artifact
    if isinstance(art, dict) and art.get("event") in RECEIPT_EVENTS:
        yield ChatEvent(type="receipt", run_id=run_id, data=art)


SOURCE_KINDS = frozenset({"fact", "document", "record", "person", "graph"})
EVIDENCE_KEYS = ("graph", "node_id", "node_label", "query")  # where in FalkorDB the source was read


def sources_of(artifact: object) -> dict[str, list[Any]]:
    """Where a tool's answer came from, for the UI: every cited source with its graph level, the levels
    searched, and the levels not opened with the reason (never a count). Empty when nothing is cited."""
    found: dict[str, dict[str, Any]] = {}
    searched: list[str] = []
    skipped: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("id"), str) and node.get("kind") in SOURCE_KINDS:
                level = node.get("level") or node.get("classification")
                found.setdefault(
                    node["id"],
                    {
                        "id": node["id"],
                        "kind": node["kind"],
                        "title": node.get("title"),
                        "level": level,
                        **{k: node[k] for k in EVIDENCE_KEYS if node.get(k)},
                    },
                )
            for key, value in node.items():
                if key == "not_searched" and isinstance(value, list):
                    skipped.extend(v for v in value if isinstance(v, dict))
                elif key == "searched" and isinstance(value, list):
                    searched.extend(v for v in value if isinstance(v, str) and v not in searched)
                else:
                    walk(value)

    walk(artifact)
    out: dict[str, list[Any]] = {}
    if found:
        out["sources"] = list(found.values())
    if searched:
        out["searched"] = searched
    if skipped:
        out["not_searched"] = skipped
    return out


def collect(session_id: str, events: list[ChatEvent]) -> TurnResult:
    run_id = events[0].run_id if events else ""
    final = next((e for e in reversed(events) if e.type in {"final", "error"}), None)
    interrupt = next((e.data for e in events if e.type == "interrupt"), None)
    if final is None or final.type == "error":
        err = final.data.get("message") if final else "no result"
        return TurnResult(run_id=run_id, session_id=session_id, answer="", status="failed", error=str(err))
    return TurnResult(
        run_id=run_id,
        session_id=session_id,
        answer=str(final.data.get("answer", "")),
        status=final.data.get("status", "done"),
        receipts=[e.data for e in events if e.type == "receipt"],
        tools=[str(e.data["tool"]) for e in events if e.type == "tool_start"],
        interrupt=interrupt,
    )


def friendly(exc: Exception) -> str:
    name = type(exc).__name__
    if "ModelCallLimit" in name or _LIMIT_HIT.search(str(exc)):
        return ABSTAIN_UNCONFIRMED
    if "RateLimit" in name or "429" in str(exc):
        return "The language model is rate-limited right now (free tier). Please retry in a minute."
    if "Timeout" in name:
        return "The language model timed out. Please retry."
    return "Something went wrong on our side; the error was logged."
