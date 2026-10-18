"""EvalRunner: one case -> persona -> session -> agent turns -> checks on artifacts, answer and memory files."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, messages_from_dict, messages_to_dict
from langchain_core.outputs import LLMResult

from nwb.agent.events import TurnResult
from nwb.evals.cases import EvalCase, PersonaSpec

if TYPE_CHECKING:
    from nwb.container import Container


_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"})


class CassetteRecorder(BaseCallbackHandler):
    """Collects every chat-model response in call order (main agent, subagents, summariser)."""

    def __init__(self) -> None:
        self.messages: list[BaseMessage] = []

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **_: Any) -> None:
        for gens in response.generations:
            for g in gens:
                msg = getattr(g, "message", None)
                if isinstance(msg, BaseMessage):
                    self.messages.append(msg)


def save_cassette(path: Path, messages: list[BaseMessage], meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = [AIMessage(content=m.content, tool_calls=getattr(m, "tool_calls", []) or []) for m in messages]
    path.write_text(json.dumps({"meta": meta, "messages": messages_to_dict(clean)}, indent=1, default=str) + "\n")


def load_cassette(path: Path) -> list[BaseMessage]:
    return list(messages_from_dict(json.loads(path.read_text())["messages"]))


@dataclass
class EvalResult:
    case_id: str
    person_id: str
    turns: list[TurnResult]
    transcript: list[BaseMessage]
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def answer(self) -> str:
        return self.turns[-1].answer if self.turns else ""

    def explain(self) -> str:
        lines = [f"case {self.case_id} as {self.person_id}: {'PASS' if self.passed else 'FAIL'}"]
        lines += [f"  !! {f}" for f in self.failures]
        lines += [f"  {ln}" for ln in render_transcript(self.transcript).splitlines()]
        return "\n".join(lines)


def render_transcript(messages: list[BaseMessage]) -> str:
    out: list[str] = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                out.append(f"-> {tc['name']}({json.dumps(tc.get('args', {}), default=str)})")
            text = m.content if isinstance(m.content, str) else str(m.content)
            if text.strip():
                out.append(f"assistant: {text.strip()}")
        elif isinstance(m, ToolMessage):
            body = m.content if isinstance(m.content, str) else str(m.content)
            out.append(f"<- {m.name} [{m.status}]: {body[:400]}")
        else:
            body = m.content if isinstance(m.content, str) else str(m.content)
            out.append(f"{m.type}: {body[:400]}")
    return "\n".join(out)


def walk(obj: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def same_value(a: Any, b: str) -> bool:
    try:
        return float(str(a)) == float(b)
    except ValueError:
        return str(a).strip().lower() == b.strip().lower()


class EvalRunner:
    def __init__(self, c: Container) -> None:
        self._c = c

    # ------------------------------------------------------------------ persona
    def person_for(self, spec: PersonaSpec, asof: date) -> str:
        if spec.id:
            return spec.id
        with self._c.db.unit_of_work() as uow:
            rows = uow.fetchall(
                "SELECT id FROM people WHERE country = :c AND status = 'active' AND NOT external ORDER BY id",
                {"c": spec.country},
            )
        for r in rows:
            pid = str(r["id"])
            has_client = bool(self._c.knowledge.scopes_for(pid, asof).get("client"))
            if spec.with_client is None or has_client == spec.with_client:
                return pid
        raise LookupError(f"no person matches {spec}")

    # ------------------------------------------------------------------ runs
    def _asof(self, case: EvalCase) -> date:
        return case.asof or self._c.agent_config.default_asof or self._c.clock.now().date()

    def _run(self, case: EvalCase, callbacks: list[Any]) -> EvalResult:
        asof = self._asof(case)
        pid = self.person_for(case.persona, asof)
        runner = self._c.runner
        sid = runner.start_session(pid, asof)
        turns = []
        base = self._c.agent
        graph = base.with_config(callbacks=callbacks) if callbacks else base
        self._c.__dict__["agent"] = graph  # the runner reads the container's graph lazily
        try:
            for text in case.turns:
                turns.append(runner.turn(pid, sid, text))
        finally:
            self._c.__dict__["agent"] = base
        state = base.get_state({"configurable": {"thread_id": sid}})
        transcript = list(state.values.get("messages", []))
        res = EvalResult(case.id, pid, turns, transcript)
        res.failures = self.check(case, res, sid)
        return res

    def run_replay(self, case: EvalCase | dict[str, Any], cassette: Path) -> EvalResult:
        """Replays recorded responses through whichever chat model the container uses (tests: a scripted one)."""
        case = case if isinstance(case, EvalCase) else EvalCase.model_validate(case)
        model = self._c.models.main
        script = getattr(model, "script", None)
        if script is None:
            raise TypeError("run_replay needs a scripted chat model in the container")
        script[:] = load_cassette(cassette)
        return self._run(case, [])

    def run_live(
        self, case: EvalCase, cassette_dir: Path | None = None, transcript_dir: Path | None = None
    ) -> EvalResult:
        rec = CassetteRecorder()
        res = self._run(case, [rec])
        meta = {"case": case.id, "person": res.person_id, "passed": res.passed, "model": str(self._c.models.main)}
        if cassette_dir is not None:
            save_cassette(cassette_dir / f"{case.id}.json", rec.messages, meta)
        if transcript_dir is not None:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            body = render_transcript(res.transcript)
            status = "PASS" if res.passed else "FAIL: " + "; ".join(res.failures)
            (transcript_dir / f"{case.id}.md").write_text(
                f"# {case.id} ({res.person_id})\n\nResult: {status}\n\n```text\n{body}\n```\n", encoding="utf-8"
            )
        return res

    # ------------------------------------------------------------------ checks
    def check(self, case: EvalCase, res: EvalResult, session_id: str) -> list[str]:
        e, fails = case.expect, []
        if not res.turns or res.turns[-1].status != "done":
            last = res.turns[-1] if res.turns else None
            return [f"run did not finish: {last.status if last else 'no turns'} {last.error if last else ''}"]
        artifacts = [m.artifact for m in res.transcript if isinstance(m, ToolMessage) and m.artifact is not None]
        called = {tc["name"] for m in res.transcript if isinstance(m, AIMessage) for tc in m.tool_calls}
        answer = res.answer.translate(_DASHES)  # models often typeset ids with non-breaking hyphens
        for t in e.tools:
            if t not in called:
                fails.append(f"tool {t} was not called (called: {sorted(called)})")
        if e.value is not None:
            if not any(k == "value" and same_value(v, e.value) for a in artifacts for k, v in walk(a)):
                fails.append(f"no tool artifact carries value {e.value!r}")
            if e.value not in answer:
                fails.append(f"answer does not state {e.value!r}")
        if e.cite is not None:
            ids = [str(v) for a in artifacts for k, v in walk(a) if k in {"id", "rule_id"} and isinstance(v, str)]
            if not any(i.startswith(e.cite) for i in ids):
                fails.append(f"no tool artifact id starts with {e.cite!r}")
            if e.cite not in answer:
                fails.append(f"answer does not cite {e.cite!r}")
        if e.memory_file is not None:
            name = e.memory_file.removeprefix("/memories/")
            text = self._c.memory_files.read(self._c.settings.company_id, res.person_id, name) or ""
            if (e.contains or "").lower() not in text.lower():
                fails.append(f"{e.memory_file} does not contain {e.contains!r}")
        return fails
