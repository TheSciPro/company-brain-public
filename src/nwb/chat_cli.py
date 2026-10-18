"""Terminal front-end for ChatService: streams tokens, shows tool calls, receipts and interrupts."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any, TextIO

from nwb.agent.clock import resolve_asof
from nwb.agent.events import ChatEvent
from nwb.container import Container

HELP = "/end = end session (writes notes)  /new = new session  /quit = exit"

Decide = Callable[[dict[str, Any]], str]  # one action request -> "approve" | "reject"


def _ask_terminal(req: dict[str, Any]) -> str:
    answer = input("  approve? [y/N] ").strip().lower()
    return "approve" if answer in {"y", "yes"} else "reject"


def _requests(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in data.get("interrupts", []):
        out.extend(item.get("action_requests", []) if isinstance(item, dict) else [])
    return out


class ChatCli:
    def __init__(
        self, c: Container, person_id: str, asof: str | None, out: TextIO = sys.stdout, decide: Decide | None = None
    ) -> None:
        self._c = c
        self._decide = decide or _ask_terminal
        self._person = person_id
        persona = c.personas.get(person_id)
        self._asof = resolve_asof(
            date.fromisoformat(asof) if asof else None,
            (persona.asof if persona else None) or c.agent_config.default_asof,
            c.clock,
        )
        self._out = out
        self._session = ""

    def _start(self) -> None:
        self._session = self._c.runner.start_session(self._person, self._asof)
        self._print(f"[session {self._session} · {self._person} · asof {self._asof}]  {HELP}")

    def _print(self, text: str, end: str = "\n") -> None:
        self._out.write(text + end)
        self._out.flush()

    def _render(self, ev: ChatEvent) -> None:
        if ev.type == "token":
            self._print(str(ev.data["text"]), end="")
        elif ev.type == "tool_start":
            self._print(f"\n  ↳ {ev.data['tool']}({ev.data.get('args', {})})")
        elif ev.type == "tool_end":
            self._print(f"  ✓ {ev.data['tool']} [{ev.data['status']}]")
        elif ev.type == "receipt":
            reason = ev.data.get("reason")
            shown = f" — {reason}" if reason else ""
            event = ev.data.get("event")
            memory_id = ev.data.get("memory_id", "")
            version = ev.data.get("version", "-")
            self._print(f"  🧾 receipt {event} {memory_id} v{version}{shown}")
        elif ev.type == "interrupt":
            for req in _requests(ev.data):
                self._print(f"\n  ⏸ approval needed: {req.get('name')} {req.get('args')}")
        elif ev.type == "final":
            self._print(f"\n[{ev.data.get('status')} · {ev.data.get('ms')} ms · run {ev.run_id}]")
        elif ev.type == "error":
            self._print(f"\n[error] {ev.data.get('message')}")

    def _run(self, events: Iterator[ChatEvent]) -> None:
        """Render a turn; on a HITL interrupt ask for decisions and resume the same run until it finishes."""
        while True:
            pending: tuple[str, list[dict[str, Any]]] | None = None
            for ev in events:
                self._render(ev)
                if ev.type == "interrupt":
                    pending = (ev.run_id, _requests(ev.data))
            if pending is None:
                return
            run_id, reqs = pending
            decisions = [{"type": self._decide(r)} for r in reqs] or [{"type": "reject"}]
            self._print(f"  → {', '.join(d['type'] for d in decisions)}")
            events = self._c.runner.resume(self._person, run_id, decisions)

    def say(self, text: str) -> None:
        self._print(f"\n{self._person}> {text}\nbrain> ", end="")
        self._run(self._c.runner.stream(self._person, self._session, text))

    def end(self) -> None:
        if self._session:
            self._c.runner.end_session(self._person, self._session)
            self._print(f"[session {self._session} ended; notes written]")

    def ask(self, questions: list[str], *, end: bool = True) -> int:
        self._start()
        for q in questions:
            self.say(q)
        if end:
            self.end()
        return 0

    def repl(self) -> int:
        self._start()
        while True:
            try:
                line = input(f"\n{self._person}> ").strip()
            except (EOFError, KeyboardInterrupt):
                self.end()
                return 0
            if not line:
                continue
            if line == "/quit":
                self.end()
                return 0
            if line in {"/end", "/new"}:
                self.end()
                if line == "/end":
                    return 0
                self._start()
                continue
            self._print("brain> ", end="")
            self._run(self._c.runner.stream(self._person, self._session, line))
