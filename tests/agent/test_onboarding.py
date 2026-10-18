"""R3 onboarding: plan from templates, brief, first-contact ritual latch, heartbeat silence/active hours/dedupe."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from nwb.agent.context import RuntimeContext
from nwb.api.app import create_app
from nwb.db.repo_directory import DirectoryReader
from nwb.db.repo_onboarding import OnboardingRepository
from nwb.onboarding.bootstrap import OnboardingBootstrap
from nwb.onboarding.flow import new_record
from nwb.onboarding.service import OnboardingService
from nwb.ports import Row
from tests.agent.conftest import ASOF, Harness
from tests.fakes import call

IST = ZoneInfo("Asia/Kolkata")
NEW_HIRE = "NW2027"  # Omkar Bhat, joined 26 Aug 2026


@pytest.fixture
def onboarded(harness: Harness) -> Harness:
    harness.c.onboarding.seed_templates()
    assert harness.c.onboarding.ensure_plan(NEW_HIRE, ASOF)
    return harness


def _system(messages: list[BaseMessage]) -> str:
    c = messages[0].content
    return c if isinstance(c, str) else " ".join(str(b.get("text", "")) for b in c)  # type: ignore[union-attr]


def test_plan_is_idempotent_and_picks_a_buddy(onboarded: Harness) -> None:
    ob = onboarded.c.onboarding
    assert ob.ensure_plan(NEW_HIRE, ASOF) is False
    plan = ob.plan(NEW_HIRE)
    assert plan is not None and plan.start_date == date(2026, 8, 26)
    assert plan.buddy_id and plan.buddy_id not in {NEW_HIRE, "NW2075"}  # never self, never the manager
    with ob._db.unit_of_work() as uow:
        hire_units = {m.unit_id for m in DirectoryReader(uow).memberships(NEW_HIRE, ASOF)}
        buddy_units = {m.unit_id for m in DirectoryReader(uow).memberships(plan.buddy_id, ASOF)}
    assert hire_units & buddy_units  # shipped config keeps buddy.same_unit on


def test_buddy_same_unit_flag_is_what_limits_the_pool(onboarded: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    ob = onboarded.c.onboarding
    seen: list[str | None] = []
    original = OnboardingRepository.buddy_candidates

    def spy(self: OnboardingRepository, exclude: list[str], hired_before: date, unit_id: str | None) -> list[Row]:
        seen.append(unit_id)
        return original(self, exclude, hired_before, unit_id)

    monkeypatch.setattr(OnboardingRepository, "buddy_candidates", spy)
    with ob._db.unit_of_work() as uow:
        units = [m.unit_id for m in DirectoryReader(uow).memberships(NEW_HIRE, ASOF)]
        assert ob._pick_buddy(uow, NEW_HIRE, ASOF)
    assert seen == [units[0]]
    wide_cfg = ob._cfg.model_copy(update={"buddy": ob._cfg.buddy.model_copy(update={"same_unit": False})})
    wide = OnboardingService(ob._db, wide_cfg, ob._company)
    with wide._db.unit_of_work() as uow:
        assert wide._pick_buddy(uow, NEW_HIRE, ASOF)
    assert seen[-1] is None


def test_brief_has_day_phase_and_people(onboarded: Harness) -> None:
    b = onboarded.c.onboarding.brief(NEW_HIRE, ASOF)
    assert b.day == 34 and b.phase == "p3"
    assert b.manager and "NW2075" in b.manager
    assert b.overdue and all(i.overdue for i in b.overdue)


def test_update_item_is_owner_scoped(onboarded: Harness) -> None:
    from nwb.errors import NotFoundError

    item = onboarded.c.onboarding.brief(NEW_HIRE, ASOF).overdue[0]
    assert onboarded.c.onboarding.update_item(NEW_HIRE, item.id, "done", "user said so").ok
    assert item.id not in {i.id for i in onboarded.c.onboarding.brief(NEW_HIRE, ASOF).overdue}
    with pytest.raises(NotFoundError):
        onboarded.c.onboarding.update_item("NW1103", item.id, "done", None)


def _pending_beats(harness: Harness, person: str) -> list[str]:
    ob = harness.c.onboarding
    beats = ob.machine_beats()
    record = new_record(beats, ob.hire_facts(person, ASOF), now=ASOF.isoformat())
    return [beat.id for beat in beats if record.beats[beat.id] == "pending"]


def _offer(beat: str) -> AIMessage:
    return AIMessage(content=f'Answered your question.\n<onboarding-beat id="{beat}"/>')


def test_a_shown_beat_stays_on_screen_until_it_is_answered(onboarded: Harness) -> None:
    first, second = _pending_beats(onboarded, NEW_HIRE)[:2]
    onboarded.model.script = [
        _offer(first),
        AIMessage(content="Still with you on that."),
        _offer(second),
    ]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    opened = svc.turn(NEW_HIRE, sid, "who is my manager?")
    assert "Answered your question." in opened.answer and "<onboarding-beat" not in opened.answer
    assert f'id="{first}"' in _system(onboarded.model.seen[-1])
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None and flow.beats[first] == "shown" and flow.beats[second] == "pending"
    held = svc.turn(NEW_HIRE, sid, "ok")
    assert "Still with you" in held.answer
    assert f'id="{first}"' in _system(onboarded.model.seen[-1])
    assert f'id="{second}"' not in _system(onboarded.model.seen[-1])
    ctx = RuntimeContext(onboarded.c.settings.company_id, NEW_HIRE, sid, "run-answer", ASOF.isoformat(), "")
    tool = onboarded.c.tools["onboarding_step"]
    runtime = ToolRuntime(state={}, context=ctx, config={}, stream_writer=lambda _: None, tool_call_id="t", store=None)
    tool.invoke(
        {
            "type": "tool_call",
            "id": "t",
            "name": "onboarding_step",
            "args": {"action": "answer", "beat": second, "note": "yes, that matches", "runtime": runtime},
        }
    )
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None and flow.beats[first] == "done" and flow.beats[second] == "pending"
    assert "yes, that matches" in (flow.picks.get(first) or "")
    nxt = svc.turn(NEW_HIRE, sid, "and the next thing?")
    assert "<onboarding-beat" not in nxt.answer
    assert f'id="{second}"' in _system(onboarded.model.seen[-1])
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None and flow.beats[second] == "shown"


def test_new_hire_chat_gets_a_beat_and_heartbeat_and_cron_do_not(onboarded: Harness) -> None:
    beat = _pending_beats(onboarded, NEW_HIRE)[0]
    onboarded.model.script = [
        AIMessage(content="NO_REPLY"),
        AIMessage(content="NO_REPLY"),
        _offer(beat),
    ]
    svc = onboarded.c.runner
    svc.scheduled(NEW_HIRE, ASOF, "heartbeat", "check in")
    svc.scheduled(NEW_HIRE, ASOF, "cron", "nightly")
    assert "onboarding-beat" not in _system(onboarded.model.seen[0])
    assert "onboarding-beat" not in _system(onboarded.model.seen[1])
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) is None
    sid = svc.start_session(NEW_HIRE, ASOF)
    svc.turn(NEW_HIRE, sid, "what should I focus on?")
    assert "onboarding-beat" in _system(onboarded.model.seen[2])
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None and flow.beats[beat] == "shown"
    again = onboarded.c.onboarding.load_flow(NEW_HIRE)
    svc.scheduled(NEW_HIRE, ASOF, "heartbeat", "check in again")
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) == again


def test_plain_answer_leaves_the_beat_pending_until_the_offer_is_in_the_reply(onboarded: Harness) -> None:
    beat = _pending_beats(onboarded, NEW_HIRE)[0]
    onboarded.model.script = [AIMessage(content="Here is the answer to your question."), _offer(beat)]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    svc.turn(NEW_HIRE, sid, "what should I focus on?")
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) is None
    assert f'id="{beat}"' in _system(onboarded.model.seen[-1])
    svc.turn(NEW_HIRE, sid, "thanks, and the beat?")
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None and flow.beats[beat] == "shown"


def test_instruction_text_inside_the_beat_tag_is_never_shown(onboarded: Harness) -> None:
    beat = _pending_beats(onboarded, NEW_HIRE)[0]
    leaked = "Confirm they joined the private guidance and do not save this as a memory."
    onboarded.model.script = [
        AIMessage(
            content=(f'Your manager is the right person.\n<onboarding-beat id="{beat}">\n{leaked}\n</onboarding-beat>')
        )
    ]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    events = list(svc.stream(NEW_HIRE, sid, "who is my manager?"))
    shown = "".join(str(event.data.get("text", "")) for event in events if event.type == "token")
    final = next(event for event in events if event.type == "final")
    answer = str(final.data["answer"])
    assert leaked not in shown and leaked not in answer
    assert "<onboarding-beat" not in shown and "<onboarding-beat" not in answer
    assert "Your manager is the right person." in answer
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) is None


def test_an_unclosed_beat_tag_is_not_stored_and_does_not_advance(onboarded: Harness) -> None:
    beat = _pending_beats(onboarded, NEW_HIRE)[0]
    leaked = "Confirm they joined the private guidance and do not save this as a memory."
    onboarded.model.script = [
        AIMessage(content=f'Your manager is the right person.\n<onboarding-beat id="{beat}">\n{leaked}')
    ]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    events = list(svc.stream(NEW_HIRE, sid, "who is my manager?"))
    shown = "".join(str(event.data.get("text", "")) for event in events if event.type == "token")
    final = next(event for event in events if event.type == "final")
    answer = str(final.data["answer"])
    assert leaked not in shown and leaked not in answer
    assert "<onboarding-beat" not in shown and "<onboarding-beat" not in answer
    assert "Your manager is the right person." in answer
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) is None


def test_a_removed_shown_beat_is_cleared_and_the_next_one_is_offered(
    onboarded: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    ob = onboarded.c.onboarding
    beats = ob.machine_beats()
    record = new_record(beats, ob.hire_facts(NEW_HIRE, ASOF), now=ASOF.isoformat())
    record = record.model_copy(update={"waiting": "retired_beat", "beats": {**record.beats, "retired_beat": "shown"}})
    ob.save_flow(NEW_HIRE, record)
    cfg = onboarded.c.agent_config
    boot = OnboardingBootstrap(
        onboarded.c.db, ob, onboarded.c.prompts, cfg.prompts.bootstrap, onboarded.c.onboarding_config
    )
    with caplog.at_level(logging.WARNING, logger="nwb.onboarding.bootstrap"):
        note = boot.note(onboarded.ctx(NEW_HIRE))
    assert note is not None
    assert note.beat_id == _pending_beats(onboarded, NEW_HIRE)[0]
    assert "retired_beat" not in note.text
    saved = ob.load_flow(NEW_HIRE)
    assert saved is not None and saved.waiting is None
    assert any("retired_beat" in rec.getMessage() for rec in caplog.records)


def test_onboarding_step_answers_the_beat_that_was_asked(onboarded: Harness) -> None:
    asked, later = _pending_beats(onboarded, NEW_HIRE)[:2]
    onboarded.model.script = [
        _offer(asked),
        call("read_file", {"file_path": "/skills/onboarding/SKILL.md", "limit": 1000}, "c0"),
        call("onboarding_step", {"action": "answer", "beat": later, "note": "yes, that matches"}, "c1"),
        AIMessage(content="Glad that matches."),
    ]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    svc.turn(NEW_HIRE, sid, "who is my manager?")
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None and flow.beats[asked] == "shown" and flow.beats[later] == "pending"
    assert f'id="{asked}"' in _system(onboarded.model.seen[-1])
    svc.turn(NEW_HIRE, sid, "yes, that matches")
    flow = onboarded.c.onboarding.load_flow(NEW_HIRE)
    assert flow is not None
    assert flow.beats[asked] == "done"
    assert flow.beats[later] == "pending"
    assert "yes, that matches" in (flow.picks.get(asked) or "")
    assert later not in flow.picks


def test_onboarding_step_before_any_offer_does_not_end_the_ritual(onboarded: Harness) -> None:
    beat = _pending_beats(onboarded, NEW_HIRE)[0]
    onboarded.model.script = [
        call("read_file", {"file_path": "/skills/onboarding/SKILL.md", "limit": 1000}, "c0"),
        call("onboarding_step", {"action": "answer", "beat": beat, "note": "yes, that matches"}, "c1"),
        AIMessage(content="Glad that matches."),
    ]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    svc.turn(NEW_HIRE, sid, "what should I focus on?")
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) is None
    plan = onboarded.c.onboarding.plan(NEW_HIRE)
    assert plan is not None and not plan.welcomed


def test_failed_turn_does_not_latch_the_ritual(onboarded: Harness) -> None:
    def boom(_: list[BaseMessage]) -> AIMessage:
        raise RuntimeError("provider down")

    svc = onboarded.c.runner
    onboarded.model.script = [boom, AIMessage(content="Welcome!")]
    sid = svc.start_session(NEW_HIRE, ASOF)
    svc.turn(NEW_HIRE, sid, "hi")
    svc.turn(NEW_HIRE, sid, "hi again")
    assert "first conversation" in _system(onboarded.model.seen[-1])


def test_bootstrap_is_not_shown_to_non_new_hires(onboarded: Harness) -> None:
    onboarded.model.script = [AIMessage(content="Hi")]
    sid = onboarded.c.runner.start_session("NW1103", ASOF)
    onboarded.c.runner.turn("NW1103", sid, "hi")
    assert "first conversation" not in _system(onboarded.model.seen[0])


def test_onboarding_step_on_a_heartbeat_run_does_not_move_the_machine(onboarded: Harness) -> None:
    ctx = RuntimeContext(onboarded.c.settings.company_id, NEW_HIRE, "hb", "run-hb", ASOF.isoformat(), "", "heartbeat")
    tool = onboarded.c.tools["onboarding_step"]
    runtime = ToolRuntime(state={}, context=ctx, config={}, stream_writer=lambda _: None, tool_call_id="t", store=None)
    out = tool.invoke(
        {
            "type": "tool_call",
            "id": "t",
            "name": "onboarding_step",
            "args": {"action": "skip_all", "runtime": runtime},
        }
    )
    assert isinstance(out, ToolMessage)
    assert out.status == "success" and "does not run" in str(out.content)
    assert onboarded.c.onboarding.load_flow(NEW_HIRE) is None
    plan = onboarded.c.onboarding.plan(NEW_HIRE)
    assert plan is not None and not plan.welcomed


def test_agent_can_mark_an_item_done(onboarded: Harness) -> None:
    item = onboarded.c.onboarding.brief(NEW_HIRE, ASOF).overdue[0]
    onboarded.model.script = [
        AIMessage(content="ok"),  # bootstrap turn
        call("read_file", {"file_path": "/skills/onboarding/SKILL.md", "limit": 1000}, "c0"),
        call("update_onboarding_item", {"item_id": item.id, "status": "done"}),
        AIMessage(content="Marked done."),
    ]
    svc = onboarded.c.runner
    sid = svc.start_session(NEW_HIRE, ASOF)
    svc.turn(NEW_HIRE, sid, "hi")
    svc.turn(NEW_HIRE, sid, f"I finished: {item.title}")
    assert item.id not in {i.id for i in onboarded.c.onboarding.brief(NEW_HIRE, ASOF).overdue}


def test_heartbeat_respects_active_hours_and_dedupes(onboarded: Harness) -> None:
    hb = onboarded.c.heartbeat
    night = hb.tick(datetime(2026, 9, 28, 22, 0, tzinfo=IST), ASOF)
    assert night.skipped == "outside active hours"
    sunday = hb.tick(datetime(2026, 9, 27, 11, 0, tzinfo=IST), ASOF)
    assert sunday.skipped
    first = hb.tick(datetime(2026, 9, 28, 10, 0, tzinfo=IST), ASOF)
    again = hb.tick(datetime(2026, 9, 28, 11, 0, tzinfo=IST), ASOF)
    assert NEW_HIRE in first.sent and NEW_HIRE in again.deduped
    with onboarded.c.db.unit_of_work() as uow:
        n = uow.fetchone("SELECT COUNT(*) AS n FROM inbox_messages WHERE person_id = :p", {"p": NEW_HIRE})
    assert n is not None and int(n["n"]) == 1


def test_heartbeat_stays_silent_when_nothing_is_due(onboarded: Harness) -> None:
    for item in onboarded.c.onboarding.brief(NEW_HIRE, ASOF).overdue:
        onboarded.c.onboarding.update_item(NEW_HIRE, item.id, "done", None)
    for item in (
        onboarded.c.onboarding.brief(NEW_HIRE, ASOF).overdue + onboarded.c.onboarding.brief(NEW_HIRE, ASOF).upcoming
    ):
        onboarded.c.onboarding.update_item(NEW_HIRE, item.id, "done", None)
    rep = onboarded.c.heartbeat.tick(datetime(2026, 9, 28, 10, 0, tzinfo=IST), ASOF)
    assert NEW_HIRE in rep.silent


def test_llm_composer_no_reply_means_silence(onboarded: Harness) -> None:
    from nwb.jobs.heartbeat import HeartbeatJob

    c = onboarded.c
    job = HeartbeatJob(
        c.db,
        c.onboarding,
        c.prompts,
        c.heartbeat_config.model_copy(update={"use_llm": True}),
        composer=lambda _: "NO_REPLY",
        actions=c.actions,
        timezone="Asia/Kolkata",
    )
    rep = job.tick(datetime(2026, 9, 28, 10, 0, tzinfo=IST), ASOF)
    assert NEW_HIRE in rep.silent and not rep.sent


def test_inbox_and_brief_over_http(onboarded: Harness) -> None:
    onboarded.c.heartbeat.tick(datetime(2026, 9, 28, 10, 0, tzinfo=IST), ASOF)
    client = TestClient(create_app(onboarded.c))
    h = {"X-NWB-Person": NEW_HIRE}
    inbox = client.get("/inbox", headers=h).json()
    assert inbox and inbox[0]["kind"] == "nudge"
    assert client.get("/inbox", headers={"X-NWB-Person": "NW1103"}).json() == []
    assert client.get("/onboarding/brief", params={"asof": "2026-09-28"}, headers=h).json()["day"] == 34
