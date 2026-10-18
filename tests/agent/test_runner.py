"""The agent end to end (one create_deep_agent graph) with a scripted model: harness wiring, files, threads."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from nwb.agent.graph import Models
from nwb.agent.memory_files import directory_inner
from nwb.agent.runner import drop_tool_cites
from nwb.identity import Actor
from nwb.llm.fallback import FallbackChatModel
from tests.agent.conftest import ASOF, Harness
from tests.fakes import call

BUILTINS = {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "write_todos", "task"}
RETIRED = {"search_memory", "add_memory", "read_memory"}


def system_text(messages: list[BaseMessage]) -> str:
    content = messages[0].content
    return content if isinstance(content, str) else " ".join(str(b.get("text", "")) for b in content)  # type: ignore[union-attr]


def answer(text: str, check: Callable[[list[BaseMessage]], None]) -> Callable[[list[BaseMessage]], AIMessage]:
    def step(messages: list[BaseMessage]) -> AIMessage:
        check(messages)
        return AIMessage(content=text)

    return step


def test_builtins_on_delete_and_execute_off_domain_tools_behind_skills(harness: Harness) -> None:
    harness.model.script = [AIMessage(content="Hello.")]
    r = harness.c.runner
    r.turn("NW1103", r.start_session("NW1103", ASOF), "hi")
    bound = set(harness.model.bound_tools)
    assert bound >= BUILTINS
    assert not bound & RETIRED
    assert not bound & {"delete", "execute"}
    assert not bound & {"company_fact", "who_approves", "propose_action", "onboarding_brief"}  # skill-gated


def test_system_prompt_has_runtime_block_memory_files_and_skills(harness: Harness) -> None:
    harness.model.script = [AIMessage(content="Hello.")]
    r = harness.c.runner
    r.turn("NW1103", r.start_session("NW1103", ASOF), "hi")
    system = system_text(harness.model.seen[0])
    assert '<runtime purpose="chat">' in system and "user_id: NW1103" in system and "today: 2026-09-28" in system
    assert "# USER.md" in system and "Operating rules" in system  # MemoryMiddleware: USER.md + AGENTS.md
    assert "company-answers" in system and "/skills/" in system  # SkillsMiddleware listing


def test_reading_a_skill_binds_its_tools(harness: Harness) -> None:
    def check(_: list[BaseMessage]) -> None:
        assert {"company_fact", "who_approves", "search_company"} <= set(harness.model.bound_tools)

    harness.model.script = [
        call("read_file", {"file_path": "/skills/company-answers/SKILL.md", "limit": 1000}),
        answer("Ready.", check),
    ]
    r = harness.c.runner
    assert r.turn("NW1103", r.start_session("NW1103", ASOF), "a policy question").status == "done"


def test_session_is_the_thread_history_comes_from_the_checkpointer(harness: Harness) -> None:
    def check(messages: list[BaseMessage]) -> None:
        humans = [m.content for m in messages if isinstance(m, HumanMessage)]
        assert humans == ["my project is Atlas", "what is my project?"]

    harness.model.script = [AIMessage(content="Noted."), answer("Atlas.", check)]
    r = harness.c.runner
    sid = r.start_session("NW1103", ASOF)
    r.turn("NW1103", sid, "my project is Atlas")
    assert r.turn("NW1103", sid, "what is my project?").answer == "Atlas."


def test_user_md_is_seeded_per_user_editable_and_isolated(harness: Harness) -> None:
    files, cid = harness.c.memory_files, harness.c.settings.company_id
    r = harness.c.runner
    sid = r.start_session("NW1103", ASOF)
    r.start_session("NW0201", ASOF)
    mine = files.read(cid, "NW1103", "USER.md") or ""
    assert "NW1103" in mine and "NW0201" not in mine
    placeholder = "- (nothing yet: ask or observe, then write it here)"
    harness.model.script = [
        call(
            "edit_file",
            {"file_path": "/memories/USER.md", "old_string": placeholder, "new_string": "- Short bullet lists."},
        ),
        AIMessage(content="Saved."),
    ]
    r.turn("NW1103", sid, "I like short bullet lists")
    assert "Short bullet lists" in (files.read(cid, "NW1103", "USER.md") or "")
    assert "Short bullet lists" not in (files.read(cid, "NW0201", "USER.md") or "")
    harness.model.script = [AIMessage(content="Hi.")]
    r.turn("NW1103", r.start_session("NW1103", ASOF), "hi")  # new session -> fresh snapshot of USER.md
    assert "Short bullet lists" in system_text(harness.model.seen[-1])


def test_user_md_directory_block_resyncs_without_touching_directives(harness: Harness) -> None:
    files, cid = harness.c.memory_files, harness.c.settings.company_id
    person = "NW1103"
    r = harness.c.runner
    sid = r.start_session(person, ASOF)
    placeholder = "- (nothing yet: ask or observe, then write it here)"
    harness.model.script = [
        call(
            "edit_file",
            {"file_path": "/memories/USER.md", "old_string": placeholder, "new_string": "- Prefer short bullets."},
        ),
        AIMessage(content="Saved."),
    ]
    r.turn(person, sid, "I prefer short bullets")
    with harness.c.db.unit_of_work() as uow:
        uow.execute("UPDATE people SET title = :t WHERE id = :id", {"t": "Directory Resync Title", "id": person})
    r.start_session(person, ASOF)
    text = files.read(cid, person, "USER.md") or ""
    inner = directory_inner(text) or ""
    assert "Directory Resync Title" in inner
    assert "Prefer short bullets" in text
    assert "Prefer short bullets" not in inner


def test_user_md_edit_rejects_a_stack_trace(harness: Harness) -> None:
    files, cid = harness.c.memory_files, harness.c.settings.company_id
    r = harness.c.runner
    sid = r.start_session("NW1103", ASOF)
    before = files.read(cid, "NW1103", "USER.md") or ""
    trace = 'Traceback (most recent call last):\n  File "worker.py", line 42, in run\nRuntimeError: boom'
    harness.model.script = [
        call(
            "edit_file",
            {
                "file_path": "/memories/USER.md",
                "old_string": "- (nothing yet: ask or observe, then write it here)",
                "new_string": trace,
            },
        ),
        AIMessage(content="I will not save that."),
    ]
    r.turn("NW1103", sid, "save this error")
    assert files.read(cid, "NW1103", "USER.md") == before


def test_org_and_skills_are_read_only(harness: Harness) -> None:
    harness.model.script = [
        call("write_file", {"file_path": "/skills/evil/SKILL.md", "content": "x"}),
        AIMessage(content="ok"),
    ]
    r = harness.c.runner
    events = list(r.stream("NW1103", r.start_session("NW1103", ASOF), "write a skill"))
    end = next(e for e in events if e.type == "tool_end")
    assert end.data["status"] == "error"
    assert not (harness.c.settings.agent_dir / "skills" / "evil").exists()


def test_hallucinated_delete_is_refused_even_though_the_tool_node_has_it(harness: Harness) -> None:
    harness.model.script = [call("delete", {"file_path": "/skills/memory-hygiene/SKILL.md"}), AIMessage(content="ok")]
    r = harness.c.runner
    events = list(r.stream("NW1103", r.start_session("NW1103", ASOF), "delete the skill file"))
    end = next(e for e in events if e.type == "tool_end")
    assert end.data["status"] == "error" and "forbidden" in str(end.data["preview"])
    assert (harness.c.settings.agent_dir / "skills" / "memory-hygiene" / "SKILL.md").exists()


def test_task_delegates_to_a_markdown_subagent(harness: Harness) -> None:
    harness.model.script = [
        call("task", {"description": "Find the answer and cite it.", "subagent_type": "researcher"}),
        AIMessage(content="Researcher says: 42 [X-1]."),  # the subagent's final message
        AIMessage(content="It is 42 [X-1]."),
    ]
    r = harness.c.runner
    events = list(r.stream("NW1103", r.start_session("NW1103", ASOF), "research this"))
    end = next(e for e in events if e.type == "tool_end" and e.data["tool"] == "task")
    assert "42" in str(end.data["preview"])
    assert next(e for e in events if e.type == "final").data["answer"] == "It is 42 [X-1]."


def test_team_move_supersedes_through_the_agent(harness: Harness) -> None:
    """Payments, then Platform: the old fact gets invalid_at and later recall returns Platform."""
    early, later = date(2026, 10, 1), date(2026, 10, 11)
    r = harness.c.runner
    harness.model.script = [
        call("write_file", {"file_path": "/memories/facts/payments.md", "content": "I work on the Payments team."}),
        AIMessage(content="Noted."),
    ]
    s1 = r.start_session("NW1103", early)
    r.turn("NW1103", s1, "I work on the Payments team.")
    r.end_session("NW1103", s1)

    harness.model.script = [
        call("write_file", {"file_path": "/memories/facts/platform.md", "content": "I moved to the Platform team."}),
        AIMessage(content="Updated."),
    ]
    s2 = r.start_session("NW1103", later)
    r.turn("NW1103", s2, "I moved to the Platform team.")
    r.end_session("NW1103", s2)

    actor = Actor(harness.c.settings.company_id, "NW1103", later)
    history = harness.memory.facts_as_of(actor, include_history=True)
    payments = [f for f in history if "Payments" in f.text and "Platform" not in f.text]
    platform = [f for f in history if "Platform" in f.text]
    assert payments and payments[0].invalid_at is not None
    assert platform and platform[0].invalid_at is None
    assert payments[0].superseded_by == platform[0].id

    def check(messages: list[BaseMessage]) -> None:
        system = system_text(messages)
        block = system.split("<guessed_memories", 1)[1].split("</guessed_memories>", 1)[0]
        assert "Platform" in block and "Payments" not in block

    harness.model.script = [
        call("grep", {"pattern": "team", "path": "/memories/facts", "output_mode": "content"}),
        answer("You are on the Platform team.", check),
    ]
    ev = list(r.stream("NW1103", r.start_session("NW1103", later), "which team am I on?"))
    preview = str(next(e for e in ev if e.type == "tool_end" and e.data["tool"] == "grep").data["preview"])
    assert "Platform" in preview and "Payments" not in preview


def test_tool_calls_are_audited_with_status(harness: Harness) -> None:
    harness.model.script = [
        call("grep", {"pattern": "team", "path": "/memories/facts", "output_mode": "content"}),
        call("read_file", {"file_path": "/memories/facts/missing.md"}, "c2"),
        AIMessage(content="Nothing yet."),
    ]
    r = harness.c.runner
    r.turn("NW1103", r.start_session("NW1103", ASOF), "what do you remember?")
    with harness.c.db.unit_of_work() as uow:
        rows = {str(x["tool"]): str(x["status"]) for x in uow.fetchall("SELECT tool, status FROM tool_calls")}
    assert rows.get("grep") == "ok" and rows.get("read_file") == "error"


class _RateLimited(FakeListChatModel):
    """First provider: ChatNVIDIA-style 429 before any token."""

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _RateLimited:  # type: ignore[override]
        del tools, kwargs
        return self

    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> Iterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        raise Exception("[429] Too Many Requests")
        yield ChatGenerationChunk(message=AIMessageChunk(content=""))

    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        raise Exception("[429] Too Many Requests")
        yield ChatGenerationChunk(message=AIMessageChunk(content=""))


class _Tokens(FakeListChatModel):
    """Second provider: several real chunks, which the runner turns into token events."""

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> _Tokens:  # type: ignore[override]
        del tools, kwargs
        return self

    def _stream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> Iterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        for part in ("Hi", " there"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))

    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        for part in ("Hi", " there"):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))


def test_two_provider_chain_emits_token_chunks(harness: Harness) -> None:
    chain = FallbackChatModel(
        [_RateLimited(responses=[""]), _Tokens(responses=["Hi there"])],
        ["nvidia", "openrouter"],
        ["nemotron", "nemotron-free"],
        "main",
    )
    harness.c.models = Models(chain, chain)
    events = list(harness.c.runner.stream("NW1103", harness.c.runner.start_session("NW1103", ASOF), "hi"))
    tokens = [str(event.data["text"]) for event in events if event.type == "token"]
    assert tokens == ["Hi", " there"]
    final = next(event for event in events if event.type == "final")
    assert final.data["answer"] == "Hi there"


def test_model_failure_becomes_a_friendly_error_not_a_crash(harness: Harness) -> None:
    def boom(_: list[BaseMessage]) -> AIMessage:
        raise RuntimeError("provider exploded")

    harness.model.script = [boom]
    r = harness.c.runner
    events = list(r.stream("NW1103", r.start_session("NW1103", ASOF), "hi"))
    err = next(e for e in events if e.type == "error")
    assert "provider exploded" not in str(err.data)


def test_fact_files_are_append_only(harness: Harness) -> None:
    harness.model.script = [
        call("write_file", {"file_path": "/memories/facts/note.md", "content": "I work on the Payments team."}),
        AIMessage(content="Saved."),
    ]
    r = harness.c.runner
    sid = r.start_session("NW1103", ASOF)
    wrote = list(r.stream("NW1103", sid, "remember my team"))
    assert next(e for e in wrote if e.type == "tool_end").data["status"] == "success"
    stored = harness.memory.facts_as_of(harness.actor(), include_history=True)
    assert stored and "Payments" in stored[-1].text
    harness.model.script = [
        call(
            "edit_file",
            {
                "file_path": f"/memories/facts/{stored[-1].id}.md",
                "old_string": "Payments",
                "new_string": "Platform",
            },
        ),
        AIMessage(content="ok"),
    ]
    edited = list(r.stream("NW1103", sid, "change that fact"))
    end = next(e for e in edited if e.type == "tool_end")
    assert end.data["status"] == "error" and "append-only" in str(end.data["preview"])
    assert harness.memory.read_fact(harness.actor(), stored[-1].id).text == stored[-1].text


def test_scheduled_heartbeat_turn_uses_its_own_thread_and_purpose(harness: Harness) -> None:
    def check(messages: list[BaseMessage]) -> None:
        assert '<runtime purpose="heartbeat">' in system_text(messages)

    harness.model.script = [answer("NO_REPLY", check)]
    res = harness.c.runner.scheduled("NW1103", ASOF, "heartbeat", "[heartbeat] check in")
    assert res.answer == "NO_REPLY" and res.session_id == "heartbeat-NW1103"


def test_an_empty_reply_is_asked_for_once_more(harness: Harness) -> None:
    """Reasoning models sometimes end a run with empty content; the user must never get a blank answer."""
    harness.model.script = [AIMessage(content=""), AIMessage(content="Saved: short bullet answers from now on.")]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", harness.actor().asof)
    events = list(svc.stream("NW1103", sid, "Remember that I like short answers."))
    final = next(e for e in events if e.type == "final")
    assert final.data["answer"] == "Saved: short bullet answers from now on."


def test_a_company_fact_answer_ends_the_turn_with_its_sentence(harness: Harness) -> None:
    """direct_reply_tools: no second model call, and no more searching after a "not on record"."""
    harness.model.script = [
        call("read_file", {"file_path": "/skills/company-answers/SKILL.md", "limit": 1000}),
        call("company_fact", {"question": "How many days a week can I work from home?"}),
        AIMessage(content="must not be reached"),
    ]
    r = harness.c.runner
    res = r.turn("NW1103", r.start_session("NW1103", ASOF), "How many days a week can I work from home?")
    assert res.status == "done" and res.answer and res.answer != "must not be reached"
    assert len(harness.model.seen) == 2


def test_tool_names_are_not_left_as_citations() -> None:
    text = "You are on day 34 [onboarding_brief]. Ask Alok【onboarding_brief】 [PEX-2026-004]."
    assert drop_tool_cites(text, {"onboarding_brief"}) == "You are on day 34. Ask Alok [PEX-2026-004]."
    assert drop_tool_cites("focus this week [onboarding_brief] .", {"onboarding_brief"}) == "focus this week."


def test_memory_file_paths_never_reach_the_reply(harness: Harness) -> None:
    """config/agent.yaml reply_redactions: file names and paths are internal, even when the model slips."""
    harness.model.script = [
        AIMessage(
            content="I read your `/memories/USER.md` file: at most three short bullet points [/memories/USER.md]."
        )
    ]
    r = harness.c.runner
    res = r.turn("NW0301", r.start_session("NW0301", ASOF), "How do you know how I like my answers?")
    assert ".md" not in res.answer and "/memories" not in res.answer, res.answer
    assert res.answer == "I read your saved preferences: at most three short bullet points.", res.answer


def test_a_redaction_does_not_leave_a_doubled_phrase(harness: Harness) -> None:
    harness.model.script = [AIMessage(content="From your saved preferences in /memories/USER.md: bullets.")]
    r = harness.c.runner
    res = r.turn("NW0301", r.start_session("NW0301", ASOF), "How do you know?")
    assert res.answer == "From your saved preferences: bullets.", res.answer
