"""Pure onboarding state machine: beats, when-predicates, apply. No database and no clock."""

from __future__ import annotations

from jinja2 import Environment, StrictUndefined

from nwb.agent.memory_files import answers_heading, merge_directory_block
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import OnboardingConfig
from nwb.onboarding.flow import (
    COMPLETE,
    PENDING,
    PHASE_SKIPPED,
    SHOWN,
    SKIPPED,
    Answered,
    Atom,
    Beat,
    Done,
    End,
    FlowRecord,
    Shown,
    Skipped,
    When,
    Wrote,
    align,
    apply,
    holds,
    new_record,
    next_open,
    next_pending,
)
from tests.conftest import ROOT

NOW = "2026-01-01"


def _beats() -> tuple[Beat, ...]:
    return (
        Beat("confirm"),
        Beat("buddy", When(all_of=(Atom("has_buddy"),))),
        Beat("brief", When(all_of=(Atom("open_items", "gt", 0),))),
        Beat("style"),
    )


def _facts(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"has_buddy": True, "open_items": 2, "day": 3}
    base.update(over)
    return base


def _fresh(**over: object) -> FlowRecord:
    return new_record(_beats(), _facts(**over), now=NOW)


def test_unfit_beats_start_skipped_and_the_record_is_not_mutated() -> None:
    beats = _beats()
    record = new_record(beats, _facts(has_buddy=False, open_items=0), now=NOW)
    assert record.beats["buddy"] == SKIPPED
    assert record.beats["brief"] == SKIPPED
    assert record.beats["confirm"] == PENDING
    before = record.model_dump()
    apply(record, Shown("confirm"), beats, _facts(has_buddy=False, open_items=0), now=NOW)
    assert record.model_dump() == before


def test_shown_keeps_that_beat_open_and_a_later_show_closes_it() -> None:
    beats, facts = _beats(), _facts()
    record = _fresh()
    shown = apply(record, Shown("confirm"), beats, facts, now=NOW)
    assert shown.record.beats["confirm"] == SHOWN
    assert shown.record.waiting == "confirm"
    assert shown.result.next_beat == "buddy"
    assert next_open(shown.record, beats) == "confirm"
    later = apply(shown.record, Shown("buddy"), beats, facts, now=NOW)
    assert later.record.beats["confirm"] == "done"
    assert later.record.beats["buddy"] == SHOWN


def test_a_waiting_beat_removed_from_config_is_not_the_open_beat() -> None:
    beats = (Beat("confirm"), Beat("style"))
    record = FlowRecord(
        beats={"retired": SHOWN, "confirm": PENDING, "style": PENDING}, waiting="retired", updated_at=NOW
    )
    assert next_open(record, beats) is None
    assert next_pending(record, beats) == "confirm"
    still_shown = record.model_copy(update={"beats": {"retired": SHOWN, "confirm": SHOWN, "style": PENDING}})
    assert next_open(still_shown, beats) == "confirm"


def test_an_answer_cannot_land_on_a_beat_that_has_not_been_asked() -> None:
    beats, facts = _beats(), _facts()
    record = apply(_fresh(), Shown("confirm"), beats, facts, now=NOW).record
    early = apply(record, Answered("buddy", "yes"), beats, facts, now=NOW)
    assert early.result.error == "that beat has not been asked yet"
    assert early.record == record
    assert record.beats["buddy"] == PENDING


def test_answer_stores_the_note_and_empty_answer_does_not_move() -> None:
    beats, facts = _beats(), _facts()
    record = apply(_fresh(), Shown("confirm"), beats, facts, now=NOW).record
    empty = apply(record, Answered("confirm", "  "), beats, facts, now=NOW)
    assert empty.result.error and empty.record == record
    answered = apply(record, Answered("confirm", " call me Nik "), beats, facts, now=NOW)
    assert answered.record.picks["confirm"] == "call me Nik"
    assert answered.record.beats["confirm"] == "done"
    assert answered.result.next_beat == "buddy"


def test_skip_and_end_and_wrote() -> None:
    beats, facts = _beats(), _facts()
    record = _fresh()
    skipped = apply(record, Skipped("confirm"), beats, facts, now=NOW)
    assert skipped.record.beats["confirm"] == SKIPPED and skipped.result.skipped
    wrote = apply(skipped.record, Wrote("buddy"), beats, facts, now=NOW)
    assert wrote.result.wrote_instead and wrote.record.beats["buddy"] == PENDING
    ended = apply(wrote.record, End(), beats, facts, now=NOW)
    assert ended.record.phase == PHASE_SKIPPED
    assert all(status in {SKIPPED, "done"} for status in ended.record.beats.values())
    refused = apply(ended.record, Shown("style"), beats, facts, now=NOW)
    assert refused.result.error and refused.record == ended.record


def test_showing_the_last_pending_beat_completes_the_ritual() -> None:
    beats = (Beat("only"),)
    record = new_record(beats, {}, now=NOW)
    step = apply(record, Shown("only"), beats, {}, now=NOW)
    assert step.record.phase == COMPLETE and step.result.completed
    assert next_pending(step.record, beats) is None


def test_done_refuses_while_a_beat_is_pending() -> None:
    beats, facts = _beats(), _facts()
    record = apply(_fresh(), Shown("confirm"), beats, facts, now=NOW).record
    refused = apply(record, Done(), beats, facts, now=NOW)
    assert refused.result.error and refused.record.phase != COMPLETE
    current = record
    for beat_id in ("buddy", "brief"):
        current = apply(current, Skipped(beat_id), beats, facts, now=NOW).record
    assert current.phase != COMPLETE and current.beats["confirm"] == SHOWN
    # Skipping the last pending beat closes the ritual; the offered beat stays shown.
    closed = apply(current, Skipped("style"), beats, facts, now=NOW)
    assert closed.record.phase == COMPLETE
    assert closed.record.beats["confirm"] == SHOWN


def test_unknown_beat_and_predicates() -> None:
    beats, facts = _beats(), _facts()
    record = _fresh()
    unknown = apply(record, Shown("nope"), beats, facts, now=NOW)
    assert unknown.result.error and unknown.record == record
    assert not holds(When(all_of=(Atom("missing"),)), facts)
    assert holds(When(all_of=(Atom("open_items", "gt", 1),)), facts)
    assert not holds(When(all_of=(Atom("open_items", "gt", 9),)), facts)
    assert holds(When(any_of=(Atom("has_buddy"), Atom("nope"))), facts)
    assert not holds(When(all_of=(Atom("day", "gt", True),)), {"day": 3})  # bool is not a number


def test_align_picks_up_a_beat_added_later() -> None:
    record = _fresh()
    extra = (*_beats(), Beat("later"))
    aligned = align(record, extra, _facts())
    assert aligned.beats["later"] == PENDING
    assert aligned.phase != COMPLETE


def test_shipped_beats_render_and_background_purposes_do_not_onboard() -> None:
    cfg = ConfigLoader(ROOT / "config").load("onboarding", OnboardingConfig)
    env = Environment(undefined=StrictUndefined, autoescape=False)
    values = {"team": "T", "manager": "M", "buddy": "B", "top_items": ["one"]}
    for beat in cfg.beats:
        env.from_string(beat.text).render(**values)
        if beat.example:
            env.from_string(beat.example).render(**values)
    assert all(beat.example.strip() for beat in cfg.beats)
    assert "chat" in cfg.onboard_purposes
    assert "heartbeat" not in cfg.onboard_purposes
    assert "cron" not in cfg.onboard_purposes
    prompt = (ROOT / "prompts" / "onboarding_bootstrap.md").read_text(encoding="utf-8")
    yaml = (ROOT / "config" / "onboarding.yaml").read_text(encoding="utf-8")
    for banned in ("update_profile", "get_onboarding_brief"):
        assert banned not in prompt
        assert banned not in yaml
    assert "onboarding_step" in prompt
    assert "onboarding_brief" in yaml
    assert "{{ example }}" in prompt
    assert "Private guidance" in prompt
    assert "You joined {{ team }}" not in prompt
    assert "You joined {{ team }}" in yaml


def test_directory_block_merge_keeps_directives() -> None:
    heading = answers_heading((ROOT / "agent" / "templates" / "USER.md").read_text(encoding="utf-8"))
    assert heading.startswith("##")
    existing = (
        "intro\n<!-- nwb:directory -->\n- Role: Old\n- Prefer short answers\n<!-- /nwb:directory -->\n"
        f"{heading}\n- already here\n"
    )
    merged = merge_directory_block(existing, "- Role: New", heading)
    assert "- Role: New" in merged and "- Role: Old" not in merged
    assert "Prefer short answers" in merged
    assert merged.index("Prefer short answers") > merged.index(heading)
    assert "already here" in merged
    assert merge_directory_block("no markers\n- Prefer x\n", "- Role: New", heading) == "no markers\n- Prefer x\n"
