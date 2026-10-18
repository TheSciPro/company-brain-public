"""SessionRepository: sessions, messages, agent runs and tool-call traces (SQL is the source of truth)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from nwb.ports import Row, UnitOfWork


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: str
    person_id: str
    asof: date | None
    turns: int
    profile_snapshot: str | None
    bootstrap_shown: bool
    ended: bool
    notes_status: str


@dataclass(frozen=True, slots=True)
class MessageRow:
    id: str
    role: str
    content: str
    created_at: str


class SessionRepository:
    """All SQL for sessions/messages/agent_runs/tool_calls. Never commits; the unit of work does."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # ---------------------------------------------------------------- sessions
    def get(self, session_id: str) -> SessionRow | None:
        r = self._uow.fetchone(
            "SELECT id, person_id, asof, turns, profile_snapshot, bootstrap_shown, ended_at, "
            "notes_status FROM sessions WHERE id = :id",
            {"id": session_id},
        )
        return _session(r) if r else None

    def create(self, session_id: str, person_id: str, asof: date, profile_snapshot: str) -> SessionRow:
        self._uow.execute(
            "INSERT INTO sessions (id, person_id, asof, profile_snapshot) VALUES (:id, :p, :a, :s)",
            {"id": session_id, "p": person_id, "a": asof, "s": profile_snapshot},
        )
        row = self.get(session_id)
        assert row is not None
        return row

    def bump_turn(self, session_id: str) -> int:
        self._uow.execute("UPDATE sessions SET turns = turns + 1 WHERE id = :id", {"id": session_id})
        r = self._uow.fetchone("SELECT turns FROM sessions WHERE id = :id", {"id": session_id})
        return int(r["turns"]) if r else 0

    def end(self, session_id: str) -> None:
        self._uow.execute(
            "UPDATE sessions SET ended_at = :t WHERE id = :id AND ended_at IS NULL", {"id": session_id, "t": _now()}
        )

    def set_notes_status(self, session_id: str, status: str) -> None:
        self._uow.execute("UPDATE sessions SET notes_status = :s WHERE id = :id", {"id": session_id, "s": status})

    def sessions_needing_notes(self, limit: int = 20) -> list[tuple[str, str]]:
        rows = self._uow.fetchall(
            "SELECT id, person_id FROM sessions WHERE ended_at IS NOT NULL AND notes_status = 'pending' "
            "AND turns > 0 ORDER BY ended_at LIMIT :n",
            {"n": limit},
        )
        return [(str(r["id"]), str(r["person_id"])) for r in rows]

    # ---------------------------------------------------------------- messages
    def add_message(self, session_id: str, person_id: str, role: str, content: str) -> str:
        mid = new_id("msg")
        self._uow.execute(
            "INSERT INTO messages (id, session_id, person_id, role, content, hash, created_at) "
            "VALUES (:id, :s, :p, :r, :c, :h, :t)",
            {
                "id": mid,
                "s": session_id,
                "p": person_id,
                "r": role,
                "c": content,
                "h": hashlib.sha256(content.encode()).hexdigest()[:16],
                "t": _now(),
            },
        )
        return mid

    def message(self, message_id: str) -> Row | None:
        return self._uow.fetchone(
            "SELECT id, session_id, person_id, role, content, created_at FROM messages WHERE id = :id",
            {"id": message_id},
        )

    def messages_around(self, message_id: str, radius: int = 2) -> list[MessageRow]:
        m = self.message(message_id)
        if m is None:
            return []
        rows = self._uow.fetchall(
            "SELECT id, role, content, created_at FROM messages WHERE session_id = :s ORDER BY created_at, id",
            {"s": m["session_id"]},
        )
        idx = next((i for i, r in enumerate(rows) if r["id"] == message_id), 0)
        window = rows[max(0, idx - radius) : idx + radius + 1]
        return [MessageRow(str(r["id"]), str(r["role"]), str(r["content"]), str(r["created_at"])) for r in window]

    def conversation(self, session_id: str) -> list[MessageRow]:
        """Every user and assistant message, oldest first. No limit: the consolidation pass trims in code."""
        rows = self._uow.fetchall(
            "SELECT id, role, content, created_at FROM messages WHERE session_id = :s AND role IN ('user','assistant') "
            "ORDER BY created_at, id",
            {"s": session_id},
        )
        return [MessageRow(str(r["id"]), str(r["role"]), str(r["content"]), str(r["created_at"])) for r in rows]

    def transcript(self, session_id: str, limit: int) -> list[MessageRow]:
        rows = self._uow.fetchall(
            "SELECT id, role, content, created_at FROM messages WHERE session_id = :s AND role IN ('user','assistant') "
            "ORDER BY created_at, id LIMIT :n",
            {"s": session_id, "n": limit},
        )
        return [MessageRow(str(r["id"]), str(r["role"]), str(r["content"]), str(r["created_at"])) for r in rows]

    # ---------------------------------------------------------------- runs + tool traces
    def start_run(self, run_id: str, session_id: str, person_id: str, trigger_message_id: str, thread_id: str) -> None:
        self._uow.execute(
            "INSERT INTO agent_runs (id, session_id, person_id, trigger_message_id, thread_id, started_at) "
            "VALUES (:id, :s, :p, :m, :th, :t)",
            {"id": run_id, "s": session_id, "p": person_id, "m": trigger_message_id, "th": thread_id, "t": _now()},
        )

    def finish_run(
        self, run_id: str, status: str, *, prompt_refs: list[str] | None = None, error: str | None = None
    ) -> None:
        self._uow.execute(
            "UPDATE agent_runs SET status = :st, finished_at = :t, prompt_refs = :pr, error = :e WHERE id = :id",
            {
                "id": run_id,
                "st": status,
                "t": _now(),
                "e": error,
                "pr": json.dumps(prompt_refs or []),
            },
        )

    def run_thread(self, run_id: str) -> tuple[str, str] | None:
        r = self._uow.fetchone("SELECT thread_id, session_id FROM agent_runs WHERE id = :id", {"id": run_id})
        return (str(r["thread_id"]), str(r["session_id"])) if r else None

    def log_tool_call(
        self,
        run_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        status: str,
        ms: int,
        side_effect: str,
        result_rows: int | None = None,
        refusal: str | None = None,
    ) -> str:
        tid = new_id("tc")
        raw = json.dumps(args, sort_keys=True, default=str)
        self._uow.execute(
            "INSERT INTO tool_calls (id, agent_run_id, tool, args, input_hash, result_rows, refusal, ms, status, "
            "side_effect, agent_id) VALUES (:id, :r, :tool, :a, :h, :n, :ref, :ms, :st, :se, 'orchestrator')",
            {
                "id": tid,
                "r": run_id,
                "tool": tool,
                "a": raw,
                "h": hashlib.sha256(raw.encode()).hexdigest()[:16],
                "n": result_rows,
                "ref": refusal,
                "ms": ms,
                "st": status,
                "se": side_effect,
            },
        )
        return tid


def _d(v: Any) -> date | None:
    if v is None:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _session(r: Row) -> SessionRow:
    return SessionRow(
        id=str(r["id"]),
        person_id=str(r["person_id"]),
        asof=_d(r["asof"]),
        turns=int(r["turns"]),
        profile_snapshot=r["profile_snapshot"],
        bootstrap_shown=bool(r["bootstrap_shown"]),
        ended=r["ended_at"] is not None,
        notes_status=str(r["notes_status"]),
    )
