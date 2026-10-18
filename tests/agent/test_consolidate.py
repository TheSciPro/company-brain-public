"""The memory-keeper runs as the consolidation agent: adds only, holds patches, skips a small batch."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from nwb.agent.subagents import load_subagent_files
from nwb.db.repo_directory import DirectoryReader
from nwb.db.repo_sessions import SessionRepository
from tests.agent.conftest import ASOF, Harness
from tests.agent.test_runner import system_text
from tests.fakes import call

_PLACEHOLDER = "- (nothing yet: ask or observe, then write it here)"
_DIRECTIVE = "<!-- observed: 2026-09-28 | status: active -->\n- Prefer short answers."


def _human(messages: list[BaseMessage]) -> str:
    for message in messages:
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def test_a_batch_under_min_messages_is_skipped(harness: Harness) -> None:
    harness.model.script = [AIMessage(content="Noted."), AIMessage(content="Noted.")]
    runner = harness.c.runner
    sid = runner.start_session("NW1103", ASOF)
    runner.turn("NW1103", sid, "I am moving to the Pune office in November")
    runner.turn("NW1103", sid, "my password: hunter2secret")
    assert len(harness.model.seen) == 2
    runner.end_session("NW1103", sid)
    assert len(harness.model.seen) == 2  # the keeper was not started
    user = harness.c.memory_files.read(harness.c.settings.company_id, "NW1103", "USER.md") or ""
    assert _PLACEHOLDER in user
    assert harness.memory.facts_as_of(harness.actor()) == []
    with harness.c.db.unit_of_work() as uow:
        session = uow.fetchone("SELECT notes_status FROM sessions WHERE id = :id", {"id": sid})
        actions = uow.fetchall("SELECT id FROM actions")
        left = uow.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('memories', 'memory_versions', 'memory_embeddings', 'session_notes')"
        )
    assert session is not None and session["notes_status"] == "done"
    assert actions == []
    assert left == []


def test_turn_counter_starts_the_keeper_and_names_the_directory(harness: Harness) -> None:
    n = harness.c.memory_config.consolidation.interval_turns
    assert n >= 2
    harness.model.script = [AIMessage(content="ok") for _ in range(n)] + [AIMessage(content="no changes")]
    runner = harness.c.runner
    sid = runner.start_session("NW1103", ASOF)
    for i in range(n - 1):
        text = "my password: hunter2secret" if i == 1 else f"message {i}"
        runner.turn("NW1103", sid, text)
    assert len(harness.model.seen) == n - 1
    assert all("Unattended" not in system_text(call_messages) for call_messages in harness.model.seen)
    runner.turn("NW1103", sid, f"message {n - 1}")
    assert len(harness.model.seen) == n + 1
    keeper = harness.model.seen[-1]
    assert "Unattended" in system_text(keeper)
    brief = _human(keeper)
    assert "message 0" in brief
    assert "hunter2" not in brief
    with harness.c.db.unit_of_work() as uow:
        names = DirectoryReader(uow).entity_names()
    assert names and names[0] in brief
    user = harness.c.memory_files.read(harness.c.settings.company_id, "NW1103", "USER.md") or ""
    assert _PLACEHOLDER in user


def test_unattended_add_holds_a_patch_for_approval(harness: Harness) -> None:
    memory = harness.c.memory_config
    harness.c.memory_config = memory.model_copy(
        update={
            "consolidation": memory.consolidation.model_copy(update={"interval_turns": 1, "min_messages": 1}),
        }
    )
    harness.c.__dict__.pop("consolidation", None)
    harness.model.script = [
        AIMessage(content="ok"),
        call(
            "edit_file",
            {"file_path": "/memories/USER.md", "old_string": _PLACEHOLDER, "new_string": _DIRECTIVE},
            "c-add",
        ),
        call(
            "edit_file",
            {
                "file_path": "/memories/USER.md",
                "old_string": "- Prefer short answers.",
                "new_string": "- Prefer long answers.",
            },
            "c-patch",
        ),
        call(
            "write_file",
            {"file_path": "/memories/facts/note.md", "content": "I work on the Payments team."},
            "c-fact",
        ),
        call(
            "write_file",
            {"file_path": "/memories/facts/pref.md", "content": "- Prefer short answers."},
            "c-bad",
        ),
        AIMessage(content="done"),
    ]
    runner = harness.c.runner
    sid = runner.start_session("NW1103", ASOF)
    assert runner.turn("NW1103", sid, "please remember this").status == "done"
    company = harness.c.settings.company_id
    user = harness.c.memory_files.read(company, "NW1103", "USER.md") or ""
    assert "Prefer short answers." in user
    assert "Prefer long answers." not in user
    assert "NW1103" in user
    facts = harness.memory.facts_as_of(harness.actor(), include_history=True)
    assert any("Payments" in fact.text for fact in facts)
    assert all("Prefer" not in fact.text for fact in facts)
    with harness.c.db.unit_of_work() as uow:
        row = uow.fetchone(
            "SELECT a.status, a.action_type, p.id AS approval_id FROM actions a "
            "JOIN approvals p ON p.action_id = a.id WHERE a.action_type = 'memory_change'"
        )
    assert row is not None
    assert row["status"] == "awaiting_approval"
    decided = harness.c.actions.decide("NW1103", str(row["approval_id"]), "approve", "apply the patch")
    assert decided.note and "applied" in decided.note
    updated = harness.c.memory_files.read(company, "NW1103", "USER.md") or ""
    assert "Prefer long answers." in updated
    assert "Prefer short answers." not in updated
    assert "NW1103" in updated
    still = harness.memory.facts_as_of(harness.actor(), include_history=True)
    assert any("Payments" in fact.text for fact in still)


def test_consolidation_reuses_the_memory_keeper(harness: Harness) -> None:
    specs = load_subagent_files(harness.c.settings.agent_dir / "subagents")
    keeper = next(spec for spec, _body in specs if spec.name == "memory-keeper")
    job = harness.c.consolidation
    assert list(job.writes) == [
        "/memories/USER.md",
        "/memories/MEMORY.md",
        "/memories/facts/**",
    ]
    assert list(job.writes) == list(keeper.writes)
    assert keeper.model == "background"
    assert not (harness.c.settings.agent_dir / "subagents" / "consolidation.md").exists()


def test_a_long_session_is_consolidated_oldest_window_first_and_nothing_is_dropped(harness: Harness) -> None:
    memory = harness.c.memory_config
    window = 4
    harness.c.memory_config = memory.model_copy(
        update={
            "consolidation": memory.consolidation.model_copy(
                update={"interval_turns": 0, "min_messages": 1, "max_messages": window}
            ),
        }
    )
    harness.c.__dict__.pop("consolidation", None)
    turns = 5  # 10 messages: windows of 4, 4 and 2
    harness.model.script = [AIMessage(content="ok") for _ in range(turns)] + [
        AIMessage(content="no changes") for _ in range(3)
    ]
    runner = harness.c.runner
    sid = runner.start_session("NW1103", ASOF)
    for i in range(turns):
        runner.turn("NW1103", sid, f"message {i}")
    assert len(harness.model.seen) == turns  # interval 0: the counter never started the keeper
    runner.end_session("NW1103", sid)
    briefs = [_human(seen) for seen in harness.model.seen[turns:]]
    assert len(briefs) == 3
    assert "message 0" in briefs[0] and "message 1" in briefs[0] and "message 2" not in briefs[0]
    assert "message 2" in briefs[1] and "message 3" in briefs[1] and "message 1" not in briefs[1]
    assert "message 4" in briefs[2] and "message 3" not in briefs[2]
    with harness.c.db.unit_of_work(as_person="NW1103") as uow:
        ordered = [m.id for m in SessionRepository(uow).conversation(sid)]
    with harness.c.db.unit_of_work() as uow:
        keys = {
            str(r["idempotency_key"]): str(r["status"])
            for r in uow.fetchall("SELECT idempotency_key, status FROM job_runs WHERE scope_id = :s", {"s": sid})
        }
        notes = uow.fetchone("SELECT notes_status FROM sessions WHERE id = :id", {"id": sid})
    assert len(ordered) == 2 * turns
    expected = {f"consolidate:{sid}:{ordered[i]}" for i in (window - 1, 2 * window - 1, len(ordered) - 1)}
    assert keys == dict.fromkeys(expected, "done")  # each key is the last message its window really included
    assert notes is not None and notes["notes_status"] == "done"
    assert harness.c.consolidation.on_session_end("NW1103", sid).status == "skip"  # nothing left, nothing re-run
    assert len(harness.model.seen) == turns + 3
