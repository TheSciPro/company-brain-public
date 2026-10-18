"""D2: wake gate makes no model call when idle, silence is dropped, active hours and owner DM come from config."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import yaml

from nwb.actions.service import ActionService
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import ActionsConfig, ActiveHours, HeartbeatConfig, WakeCheckConfig, WakeGateConfig
from nwb.jobs.heartbeat import HeartbeatJob
from nwb.jobs.watcher import WatcherJob
from tests.agent.conftest import ASOF, Harness
from tests.conftest import ROOT

NEW_HIRE = "NW2027"
OTHER = "NW1103"
IST = ZoneInfo("Asia/Kolkata")
MONDAY_10 = datetime(2026, 9, 28, 10, 0, tzinfo=IST)


def _hire(harness: Harness) -> Harness:
    harness.c.onboarding.seed_templates()
    harness.c.onboarding.ensure_plan(NEW_HIRE, ASOF)
    return harness


def _job(
    h: Harness,
    compose: Callable[[str], str],
    cfg: HeartbeatConfig | None = None,
    *,
    timezone: str | None = None,
    loader: ConfigLoader | None = None,
) -> HeartbeatJob:
    c = h.c
    return HeartbeatJob(
        c.db,
        c.onboarding,
        c.prompts,
        (cfg or c.heartbeat_config).model_copy(update={"use_llm": True}),
        compose,
        actions=c.actions,
        timezone=timezone or c.agent_config.timezone,
        loader=loader,
    )


def _close_open_items(h: Harness) -> None:
    overdue, upcoming = h.c.onboarding.open_items(NEW_HIRE, ASOF)
    for item in (*overdue, *upcoming):
        h.c.onboarding.update_item(NEW_HIRE, item.id, "done", None)


def test_wake_gate_makes_zero_llm_calls_when_idle_or_unchanged(harness: Harness) -> None:
    h = _hire(harness)
    calls: list[str] = []

    def compose(prompt: str) -> str:
        calls.append(prompt)
        return "One item needs you."

    job = _job(h, compose, timezone="Asia/Calcutta")
    first = job.tick(MONDAY_10, ASOF)
    assert NEW_HIRE in first.sent and len(calls) == 1
    with h.c.db.unit_of_work() as uow:
        inbox = uow.fetchall("SELECT person_id, kind, title, body, ref FROM inbox_messages")
        nudges = uow.fetchall("SELECT person_id, channel, item_id FROM nudges ORDER BY id")
    assert {row["person_id"] for row in inbox} == {NEW_HIRE}
    assert inbox[0]["kind"] == "nudge" and inbox[0]["title"] == "Onboarding reminder"
    assert inbox[0]["ref"] in {row["item_id"] for row in nudges}
    assert {row["channel"] for row in nudges} == {"inbox"}
    with h.c.db.unit_of_work(as_person=OTHER) as uow:
        assert uow.fetchall("SELECT id FROM inbox_messages WHERE person_id = :p", {"p": OTHER}) == []

    again = job.tick(MONDAY_10.replace(hour=11), ASOF)
    assert NEW_HIRE in again.deduped and len(calls) == 1

    _close_open_items(h)
    quiet = job.tick(MONDAY_10.replace(hour=12), ASOF)
    assert NEW_HIRE in quiet.silent and len(calls) == 1


def test_two_silence_ticks_call_the_model_once(harness: Harness) -> None:
    h = _hire(harness)
    calls: list[str] = []

    def compose(prompt: str) -> str:
        calls.append(prompt)
        return "NO_REPLY"

    job = _job(h, compose, timezone="Asia/Calcutta")
    first = job.tick(MONDAY_10, ASOF)
    second = job.tick(MONDAY_10.replace(hour=11), ASOF)
    assert NEW_HIRE in first.silent and not first.sent
    assert NEW_HIRE in second.deduped and not second.sent
    assert len(calls) == 1
    with h.c.db.unit_of_work() as uow:
        assert uow.fetchall("SELECT id FROM inbox_messages") == []
        row = uow.fetchone("SELECT status FROM job_runs WHERE idempotency_key LIKE 'hb:%'")
    assert row is not None and row["status"] == "skipped"


def test_active_hours_follow_config_including_timezone(harness: Harness) -> None:
    h = _hire(harness)
    calls: list[int] = []

    def compose(prompt: str) -> str:
        calls.append(1)
        return "A nudge."

    # 12:30 UTC is 08:30 in New York (still on daylight time in September): before the configured 09:00.
    # 14:00 UTC is 10:00 there, inside 09:00–19:00. The window is the config, read in the given timezone.
    eastern = _job(h, compose, timezone="America/New_York")
    before = eastern.tick(datetime(2026, 9, 28, 12, 30, tzinfo=UTC), ASOF)
    assert before.skipped == "outside active hours" and calls == []
    inside = eastern.tick(datetime(2026, 9, 28, 14, 0, tzinfo=UTC), ASOF)
    assert inside.skipped is None and calls == [1]

    narrow = h.c.heartbeat_config.model_copy(
        update={"active_hours": ActiveHours(start="22:00", end="23:00"), "weekdays_only": False}
    )
    shifted = _job(h, compose, narrow, timezone="UTC")
    assert shifted.tick(datetime(2026, 9, 28, 10, 0, tzinfo=UTC), ASOF).skipped == "outside active hours"
    assert calls == [1]

    weekend = h.c.heartbeat_config.model_copy(update={"weekdays_only": True})
    sunday = _job(h, compose, weekend, timezone="Asia/Calcutta")
    assert sunday.tick(datetime(2026, 9, 27, 11, 0, tzinfo=IST), ASOF).skipped == "outside active hours (weekend)"


def test_active_hours_reload_when_the_yaml_changes(harness: Harness, tmp_path) -> None:
    h = _hire(harness)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    target = cfg_dir / "heartbeat.yaml"
    shutil.copy(ROOT / "config" / "heartbeat.yaml", target)
    target.write_text(_with_llm(target.read_text(encoding="utf-8"), True), encoding="utf-8")
    loader = ConfigLoader(cfg_dir)
    calls: list[int] = []

    def compose(prompt: str) -> str:
        calls.append(1)
        return "A nudge."

    job = _job(h, compose, loader.load("heartbeat", HeartbeatConfig), timezone="Asia/Calcutta", loader=loader)
    assert job.tick(MONDAY_10, ASOF).skipped is None and calls == [1]

    text = target.read_text(encoding="utf-8").replace(
        '{start: "09:00", end: "19:00"}', '{start: "22:00", end: "23:00"}'
    )
    target.write_text(text, encoding="utf-8")
    assert job.tick(MONDAY_10.replace(hour=11), ASOF).skipped == "outside active hours"
    assert calls == [1]


def _with_llm(text: str, on: bool) -> str:
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.startswith("use_llm:")]
    assert len(hits) == 1, "heartbeat.yaml has one top-level use_llm"
    lines[hits[0]] = f"use_llm: {'true' if on else 'false'}\n"
    return "".join(lines)


def test_use_llm_reloads_between_ticks_both_ways(harness: Harness, tmp_path) -> None:
    """use_llm is read from the reloaded yaml each tick; the composer is used only while it is true."""
    h = _hire(harness)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    target = cfg_dir / "heartbeat.yaml"
    base = (ROOT / "config" / "heartbeat.yaml").read_text(encoding="utf-8")
    target.write_text(_with_llm(base, False), encoding="utf-8")
    loader = ConfigLoader(cfg_dir)
    calls: list[str] = []

    def compose(prompt: str) -> str:
        calls.append(prompt)
        return "From the model."

    c = h.c
    job = HeartbeatJob(
        c.db,
        c.onboarding,
        c.prompts,
        loader.load("heartbeat", HeartbeatConfig),
        compose,
        actions=c.actions,
        timezone="Asia/Calcutta",
        loader=loader,
    )
    hours = iter(range(10, 19))

    def tick_body() -> str:
        when = MONDAY_10.replace(hour=next(hours))
        with h.c.db.unit_of_work() as uow:
            uow.execute("DELETE FROM nudges")
            uow.execute("DELETE FROM inbox_messages")
            uow.execute("DELETE FROM job_runs")
        report = job.tick(when, ASOF)
        assert NEW_HIRE in report.sent
        with h.c.db.unit_of_work() as uow:
            row = uow.fetchone("SELECT body FROM inbox_messages WHERE person_id = :p", {"p": NEW_HIRE})
        assert row is not None
        return str(row["body"])

    assert tick_body() != "From the model." and calls == []
    target.write_text(_with_llm(base, True), encoding="utf-8")
    assert tick_body() == "From the model." and len(calls) == 1
    target.write_text(_with_llm(base, False), encoding="utf-8")
    assert tick_body() != "From the model." and len(calls) == 1


def test_approval_nudge_takes_its_title_from_the_probe_and_leaves_the_item_empty(harness: Harness) -> None:
    h = _hire(harness)
    h.c.actions.propose(
        kind_name="send_reminder",
        requester=NEW_HIRE,
        target=OTHER,
        title="Please review the brief",
        args={"message": "A reminder"},
        asof=ASOF,
        proposed_by="agent",
    )
    cfg = h.c.heartbeat_config.model_copy(
        update={"wake_gate": WakeGateConfig(checks=(WakeCheckConfig(name="pending_approvals"),))}
    )
    calls: list[str] = []

    def compose(prompt: str) -> str:
        calls.append(prompt)
        return "An approval is waiting."

    job = _job(h, compose, cfg, timezone="Asia/Calcutta")
    report = job.tick(MONDAY_10, ASOF)
    assert NEW_HIRE in report.sent and len(calls) == 1
    with h.c.db.unit_of_work() as uow:
        nudges = uow.fetchall(
            "SELECT title, ref FROM inbox_messages WHERE kind = 'nudge' AND person_id = :p",
            {"p": NEW_HIRE},
        )
    assert len(nudges) == 1
    assert nudges[0]["title"] == "Approval waiting"
    assert nudges[0]["ref"] is None


def test_watcher_reloads_changed_yaml_and_logs_what_it_left(harness: Harness, tmp_path, caplog) -> None:
    cfg_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg_dir)
    loader = ConfigLoader(cfg_dir)
    cfg = loader.load("actions", ActionsConfig)
    actions = ActionService(harness.c.db, cfg)
    job = WatcherJob(harness.c.db, actions, cfg, loader=loader)

    with caplog.at_level(logging.INFO, logger="nwb.jobs.watcher"):
        first = job.run(ASOF)
        assert first.reloaded == []
        assert "actions" in first.unchanged and "heartbeat" in first.unchanged
        assert "reloaded=-" in caplog.text and "unchanged=" in caplog.text
        assert "heartbeat" in caplog.text

        raw = yaml.safe_load((cfg_dir / "actions.yaml").read_text(encoding="utf-8"))
        raw["watcher"]["enabled"] = False
        (cfg_dir / "actions.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        caplog.clear()
        second = job.run(ASOF)
        assert second.reloaded == ["actions"]
        assert "heartbeat" in second.unchanged
        assert second.skipped == ["watcher disabled"]
        assert "reloaded=actions" in caplog.text and "unchanged=" in caplog.text and "heartbeat" in caplog.text

        caplog.clear()
        third = job.run(ASOF)
        assert third.reloaded == [] and "actions" in third.unchanged
        assert "reloaded=-" in caplog.text
