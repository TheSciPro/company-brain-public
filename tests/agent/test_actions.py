"""R4: approval queue (watcher proposals, approver-only decisions) and the propose_action HITL interrupt."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from nwb.agent.graph import Models
from nwb.api.app import create_app
from nwb.conf import ConfigLoader, MemoryConfig
from nwb.db.repo_onboarding import InboxRepository
from nwb.errors import AccessDeniedError, ConflictError
from nwb.llm.settings import Role
from nwb.memory.edit_learning import EditLearning
from nwb.memory.phrasing import LLMPhraser, PhraseRecorder
from tests.agent.conftest import ASOF, Harness
from tests.conftest import ROOT
from tests.fakes import ScriptedChatModel, call

NEW_HIRE, MANAGER = "NW2027", "NW2075"


@pytest.fixture
def onboarded(harness: Harness) -> Harness:
    harness.c.onboarding.seed_templates()
    harness.c.onboarding.ensure_plan(NEW_HIRE, ASOF)
    return harness


def _inbox(h: Harness, person: str) -> list[str]:
    with h.c.db.unit_of_work(as_person=person) as uow:
        return [f"{m.kind}:{m.title}" for m in InboxRepository(uow).list(person)]


def _count(h: Harness, sql: str) -> int:
    with h.c.db.unit_of_work() as uow:
        row = uow.fetchone(sql)
    return int(row["n"]) if row else 0


def test_watcher_queues_overdue_onboarding_for_the_manager_once(onboarded: Harness) -> None:
    rep = onboarded.c.watcher.run(ASOF)
    assert rep.proposed and not rep.already
    again = onboarded.c.watcher.run(ASOF)
    assert not again.proposed and set(again.already) == set(rep.proposed)  # dedupe key per person and week
    pending = onboarded.c.actions.pending_for(MANAGER)
    assert [a.kind for a in pending] == ["escalate_onboarding"] and pending[0].target == NEW_HIRE
    assert any(t.startswith("approval:") for t in _inbox(onboarded, MANAGER))


def test_expiring_grant_goes_to_the_resource_owner(onboarded: Harness) -> None:
    onboarded.c.watcher.run(date(2026, 10, 1))
    with onboarded.c.db.unit_of_work() as uow:
        rows = uow.fetchall(
            "SELECT a.args, p.approver_id, d.owner_person_id FROM actions a JOIN approvals p ON p.action_id = a.id "
            "JOIN documents d ON a.args LIKE '%' || d.id || '%' WHERE a.action_type = 'renew_access'"
        )
    assert rows and all(r["approver_id"] == r["owner_person_id"] for r in rows)


def test_only_the_approver_decides_and_only_once(onboarded: Harness) -> None:
    onboarded.c.watcher.run(ASOF)
    ap = onboarded.c.actions.pending_for(MANAGER)[0]
    assert ap.approval_id
    with pytest.raises(AccessDeniedError):
        onboarded.c.actions.decide(NEW_HIRE, ap.approval_id, "approve", None)
    done = onboarded.c.actions.decide(MANAGER, ap.approval_id, "approve", "on it")
    assert done.status == "done" and done.note and NEW_HIRE in done.note
    assert any(t.startswith("system:Overdue onboarding") for t in _inbox(onboarded, NEW_HIRE))
    with pytest.raises(ConflictError):
        onboarded.c.actions.decide(MANAGER, ap.approval_id, "reject", None)


def test_rejected_action_never_executes(onboarded: Harness) -> None:
    onboarded.c.watcher.run(ASOF)
    ap = onboarded.c.actions.pending_for(MANAGER)[0]
    assert ap.approval_id
    out = onboarded.c.actions.decide(MANAGER, ap.approval_id, "reject", "not now")
    assert out.status == "rejected" and out.note is None
    assert not any(t.startswith("system:") for t in _inbox(onboarded, NEW_HIRE))


READ_SKILL = call("read_file", {"file_path": "/skills/actions/SKILL.md", "limit": 1000}, "c0")
REMIND = call(
    "propose_action",
    {"kind": "send_reminder", "target_person_id": "NW1101", "title": "Review the Q3 deck", "message": "Please review"},
)


def test_propose_action_pauses_for_the_human_then_executes_on_approve(harness: Harness) -> None:
    svc = harness.c.runner
    harness.model.script = [READ_SKILL, REMIND, AIMessage(content="Sent the reminder.")]
    sid = svc.start_session("NW1103", ASOF)
    paused = svc.turn("NW1103", sid, "remind NW1101 to review the Q3 deck")
    assert paused.status == "interrupted" and paused.interrupt
    req = paused.interrupt["interrupts"][0]["action_requests"][0]
    assert req["name"] == "propose_action" and req["args"]["target_person_id"] == "NW1101"
    assert _count(harness, "SELECT COUNT(*) AS n FROM actions") == 0  # nothing ran before the human said yes

    events = list(svc.resume("NW1103", paused.run_id, [{"type": "approve"}]))
    assert next(e for e in events if e.type == "final").data["status"] == "done"
    assert _count(harness, "SELECT COUNT(*) AS n FROM actions WHERE status = 'done'") == 1
    assert "system:Review the Q3 deck" in _inbox(harness, "NW1101")
    assert not any(t.startswith("approval:") for t in _inbox(harness, "NW1103"))  # self-approved in chat


FRIDAY = "Please review by Friday, in one short paragraph."
MONDAY = "Please review by Monday, in a single sentence."
PHRASES = ROOT / "tests" / "fixtures" / "llm_calls" / "directive_phrase"


def _learn_from_fixtures(harness: Harness) -> None:
    """The phrasing model is the configured one; this test replays its recorded calls."""
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig).edit_learning
    phraser = LLMPhraser(ScriptedChatModel(), harness.c.prompts, cfg, PhraseRecorder(PHRASES, "replay"))
    harness.c.edit_learning = EditLearning(harness.c.memory_files, phraser, cfg, harness.c.profile_gate())


def _keep_summariser_off_the_agent_script(harness: Harness) -> None:
    """The harness chat hook serves every role. Summarisation must not consume the agent's scripted turns."""
    background = ScriptedChatModel(script=[AIMessage(content="Earlier conversation, condensed.") for _ in range(8)])

    def chat(ref: object, role: object = None) -> ScriptedChatModel:
        return background if role == Role.BACKGROUND else harness.model

    harness.c.llm.chat = chat  # type: ignore[method-assign]
    # The summariser is built from ``models.background``; source subagents make it fire within a few turns.
    harness.c.models = Models(harness.model, background)


def _bodies(h: Harness, person: str) -> list[str]:
    with h.c.db.unit_of_work(as_person=person) as uow:
        rows = uow.fetchall("SELECT body FROM inbox_messages WHERE person_id = :p ORDER BY id", {"p": person})
    return [str(r["body"]) for r in rows]


def test_edit_then_approve_writes_a_prefer_directive_and_a_later_edit_supersedes_it(harness: Harness) -> None:
    _keep_summariser_off_the_agent_script(harness)
    _learn_from_fixtures(harness)
    svc = harness.c.runner
    second = call(
        "propose_action",
        {"kind": "send_reminder", "target_person_id": "NW1101", "title": "Review the Q3 deck", "message": FRIDAY},
        "c2",
    )
    harness.model.script = [
        READ_SKILL,
        REMIND,
        AIMessage(content="Sent the reminder."),
        second,
        AIMessage(content="Sent the shorter reminder."),
    ]
    sid = svc.start_session("NW1103", ASOF)
    paused = svc.turn("NW1103", sid, "remind NW1101 to review the Q3 deck")
    draft = paused.interrupt["interrupts"][0]["action_requests"][0]["args"]
    edited = {**draft, "message": FRIDAY}
    done = list(
        svc.resume(
            "NW1103",
            paused.run_id,
            [{"type": "edit", "edited_action": {"name": "propose_action", "args": edited}}],
        )
    )
    assert next(e for e in done if e.type == "final").data["status"] == "done", [e.type for e in done]
    assert FRIDAY in _bodies(harness, "NW1101")

    user_md = harness.c.memory_files.read(harness.c.settings.company_id, "NW1103", "USER.md") or ""
    assert "Prefer: Ask for the review by Friday and keep reminder messages to one short paragraph." in user_md
    assert f"observed: {ASOF.isoformat()} | status: active | scope: send_reminder.message" in user_md

    again = svc.turn("NW1103", sid, "remind them again")
    assert again.interrupt, (again.status, again.tools, again.answer, again.error)
    draft2 = again.interrupt["interrupts"][0]["action_requests"][0]["args"]
    list(
        svc.resume(
            "NW1103",
            again.run_id,
            [{"type": "edit", "edited_action": {"name": "propose_action", "args": {**draft2, "message": MONDAY}}}],
        )
    )
    updated = harness.c.memory_files.read(harness.c.settings.company_id, "NW1103", "USER.md") or ""
    assert "status: superseded | scope: send_reminder.message" in updated
    assert updated.index("superseded | scope: send_reminder.message") < updated.index(
        "status: active | scope: send_reminder.message"
    )
    assert "single sentence" in updated
    assert MONDAY in _bodies(harness, "NW1101")


def test_edited_message_is_what_lands_in_the_inbox(harness: Harness) -> None:
    """A reviewed edit runs even when the skill was never read, and the inbox gets that message.

    Live runs called propose_action before reading its skill. The tool is not on the tool
    node until then, so resume logged the original args as an error and wrote no inbox row.
    """
    _keep_summariser_off_the_agent_script(harness)
    _learn_from_fixtures(harness)
    original = "Please review"
    harness.model.script = [
        call(
            "propose_action",
            {"kind": "send_reminder", "target_person_id": "NW1101", "title": "Review the Q3 deck", "message": original},
            "c9",
        ),
        AIMessage(content="Sent the reminder."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", ASOF)
    paused = svc.turn("NW1103", sid, "remind NW1101 to review the deck")
    assert paused.interrupt, (paused.status, paused.tools, paused.answer, paused.error)
    draft = paused.interrupt["interrupts"][0]["action_requests"][0]["args"]
    assert draft["message"] == original
    events = list(
        svc.resume(
            "NW1103",
            paused.run_id,
            [{"type": "edit", "edited_action": {"name": "propose_action", "args": {**draft, "message": FRIDAY}}}],
        )
    )
    final = next(e for e in events if e.type == "final")
    assert final.data["status"] == "done", [e.type for e in events]
    assert _bodies(harness, "NW1101") == [FRIDAY]
    with harness.c.db.unit_of_work() as uow:
        rows = uow.fetchall(
            "SELECT args, status, side_effect FROM tool_calls WHERE tool = 'propose_action' ORDER BY id"
        )
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["side_effect"] == "write_action"
    assert json.loads(rows[0]["args"])["message"] == FRIDAY


def test_a_rejected_directive_is_a_receipt_and_the_edited_action_still_runs(harness: Harness) -> None:
    """A venting Prefer line is refused out loud. The approved action has already run."""
    _keep_summariser_off_the_agent_script(harness)
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig).edit_learning

    class _Vent:
        def phrase(self, request: object) -> str:
            return "ugh I hate this Friday"

    harness.c.edit_learning = EditLearning(harness.c.memory_files, _Vent(), cfg, harness.c.profile_gate())
    edited_message = "Please review Friday"
    harness.model.script = [
        call(
            "propose_action",
            {
                "kind": "send_reminder",
                "target_person_id": "NW1101",
                "title": "Review the Q3 deck",
                "message": "Please review",
            },
            "c9",
        ),
        AIMessage(content="Sent the reminder."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", ASOF)
    paused = svc.turn("NW1103", sid, "remind NW1101 to review the deck")
    assert paused.interrupt
    draft = paused.interrupt["interrupts"][0]["action_requests"][0]["args"]
    events = list(
        svc.resume(
            "NW1103",
            paused.run_id,
            [
                {
                    "type": "edit",
                    "edited_action": {"name": "propose_action", "args": {**draft, "message": edited_message}},
                }
            ],
        )
    )
    final = next(e for e in events if e.type == "final")
    assert final.data["status"] == "done"
    receipt = next(e for e in events if e.type == "receipt" and e.data.get("event") == "REJECTED")
    assert events.index(final) < events.index(receipt)
    assert "rejected (venting)" in receipt.data["reason"]
    assert "ugh" not in receipt.data["reason"]
    user_md = harness.c.memory_files.read(harness.c.settings.company_id, "NW1103", "USER.md") or ""
    assert "ugh" not in user_md
    assert _bodies(harness, "NW1101") == [edited_message]


def test_a_mixed_edit_shows_the_stored_line_and_the_refusal(harness: Harness) -> None:
    """A stored field and a refused field are separate receipts. The approval stays done."""
    _keep_summariser_off_the_agent_script(harness)
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig).edit_learning

    class _ByField:
        def phrase(self, request: object) -> str:
            field = getattr(request, "field", "")
            if field == "message":
                return "ugh I hate this Friday"
            return "Put Status in the title."

    harness.c.edit_learning = EditLearning(harness.c.memory_files, _ByField(), cfg, harness.c.profile_gate())
    harness.model.script = [
        call(
            "propose_action",
            {
                "kind": "send_reminder",
                "target_person_id": "NW1101",
                "title": "Review the Q3 deck",
                "message": "Please review",
            },
            "c9",
        ),
        AIMessage(content="Sent the reminder."),
    ]
    svc = harness.c.runner
    sid = svc.start_session("NW1103", ASOF)
    paused = svc.turn("NW1103", sid, "remind NW1101 to review the deck")
    assert paused.interrupt
    draft = paused.interrupt["interrupts"][0]["action_requests"][0]["args"]
    events = list(
        svc.resume(
            "NW1103",
            paused.run_id,
            [
                {
                    "type": "edit",
                    "edited_action": {
                        "name": "propose_action",
                        "args": {**draft, "message": "Please review Friday", "title": "Status"},
                    },
                }
            ],
        )
    )
    final = next(e for e in events if e.type == "final")
    assert final.data["status"] == "done"
    learned = [e.data for e in events if e.type == "receipt"]
    assert events.index(final) < events.index(next(e for e in events if e.type == "receipt"))
    stored = next(item for item in learned if item["event"] == "DIRECTIVE")
    refused = next(item for item in learned if item["event"] == "REJECTED")
    assert stored["scope"] == "send_reminder.title"
    assert "rejected (venting)" in refused["reason"]
    assert "ugh" not in refused["reason"]
    user_md = harness.c.memory_files.read(harness.c.settings.company_id, "NW1103", "USER.md") or ""
    assert "Put Status in the title." in user_md
    assert "ugh" not in user_md
    assert "Please review Friday" in _bodies(harness, "NW1101")


def test_propose_action_rejected_in_chat_does_nothing(harness: Harness) -> None:
    svc = harness.c.runner
    harness.model.script = [READ_SKILL, REMIND, AIMessage(content="OK, I won't send it.")]
    sid = svc.start_session("NW1103", ASOF)
    paused = svc.turn("NW1103", sid, "remind NW1101")
    list(svc.resume("NW1103", paused.run_id, [{"type": "reject", "message": "no"}]))
    assert _count(harness, "SELECT COUNT(*) AS n FROM actions") == 0
    assert _inbox(harness, "NW1101") == []


def test_approvals_api(onboarded: Harness) -> None:
    onboarded.c.watcher.run(ASOF)
    client = TestClient(create_app(onboarded.c))
    hdr = {"X-NWB-Person": MANAGER}
    items = client.get("/approvals", headers=hdr).json()
    assert len(items) == 1
    url = f"/approvals/{items[0]['approval_id']}/decide"
    assert client.post(url, json={"decision": "approve"}, headers={"X-NWB-Person": NEW_HIRE}).status_code == 403
    assert client.post(url, json={"decision": "approve"}, headers=hdr).json()["status"] == "done"
    assert client.post(url, json={"decision": "approve"}, headers=hdr).status_code == 409
    assert client.post(url, json={"decision": "maybe"}, headers=hdr).status_code == 422
