"""Onboarding as a pure state machine (Hermes ``agent/onboarding_flow.py``).

No I/O and no clock. Beats and their ``when`` predicates come from ``config/onboarding.yaml``;
``apply(record, event, beats, facts, now=...)`` returns the next record and what the model should
read. A beat moves ``pending → shown → done | skipped``. A predicate that is false skips the beat.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

PENDING, SHOWN, DONE, SKIPPED = "pending", "shown", "done", "skipped"
ONBOARDING, COMPLETE, PHASE_SKIPPED = "onboarding", "complete", "skipped"
_ENDED = "Onboarding is over in this chat; it is a normal chat now."
_OPS = frozenset({"truthy", "falsy", "eq", "ne", "gt", "gte", "lt", "lte"})


class FlowRecord(BaseModel):
    """The persisted onboarding run. JSON on the plan row; this module never writes it."""

    model_config = ConfigDict(frozen=True)

    phase: str = ONBOARDING
    beats: dict[str, str]
    picks: dict[str, str | None] = Field(default_factory=dict)
    waiting: str | None = None
    ended_by: str | None = None
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class Atom:
    """One comparison against a fact the caller computed (``has_buddy``, ``open_items``, ...)."""

    fact: str
    op: str = "truthy"
    value: object = None


@dataclass(frozen=True, slots=True)
class When:
    """Empty means the beat always applies. ``all_of`` and ``any_of`` both have to hold when set."""

    all_of: tuple[Atom, ...] = ()
    any_of: tuple[Atom, ...] = ()


@dataclass(frozen=True, slots=True)
class Beat:
    id: str
    when: When = When()


@dataclass(frozen=True, slots=True)
class Shown:
    beat: str


@dataclass(frozen=True, slots=True)
class Answered:
    beat: str
    note: str


@dataclass(frozen=True, slots=True)
class Skipped:
    beat: str


@dataclass(frozen=True, slots=True)
class Wrote:
    """The user sent their own task instead of answering. The beat stays where it was."""

    beat: str


@dataclass(frozen=True, slots=True)
class End:
    """Skip the rest of the ritual (``skip_all``)."""

    outcome: str = "skipped"


@dataclass(frozen=True, slots=True)
class Done:
    pass


Event = Shown | Answered | Skipped | Wrote | End | Done


class StepView(BaseModel):
    model_config = ConfigDict(frozen=True)

    beat: str | None = None
    next_beat: str | None = None
    skipped: bool = False
    completed: bool = False
    wrote_instead: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Step:
    record: FlowRecord
    result: StepView
    reminder: str = ""


def holds(when: When, facts: Mapping[str, object]) -> bool:
    """True when every ``all_of`` atom matches and, if any are listed, one ``any_of`` atom matches."""
    if when.all_of and not all(_atom(atom, facts) for atom in when.all_of):
        return False
    return not when.any_of or any(_atom(atom, facts) for atom in when.any_of)


def align(record: FlowRecord, beats: Sequence[Beat], facts: Mapping[str, object]) -> FlowRecord:
    """Add beats the config gained, and skip pending beats whose predicate is false now."""
    statuses = dict(record.beats)
    for beat in beats:
        statuses.setdefault(beat.id, PENDING)
        if statuses[beat.id] == PENDING and not holds(beat.when, facts):
            statuses[beat.id] = SKIPPED
    phase = record.phase
    ended = record.ended_by
    if phase == ONBOARDING and not _any_pending(statuses, beats):
        phase, ended = COMPLETE, ended or "done"
    return record.model_copy(update={"phase": phase, "beats": statuses, "ended_by": ended})


def new_record(beats: Sequence[Beat], facts: Mapping[str, object], *, now: str) -> FlowRecord:
    blank = FlowRecord(beats={beat.id: PENDING for beat in beats}, updated_at=now)
    return align(blank, beats, facts)


def next_pending(record: FlowRecord, beats: Sequence[Beat]) -> str | None:
    """The next beat that has not been offered yet."""
    return next((beat.id for beat in beats if record.beats.get(beat.id) == PENDING), None)


def next_open(record: FlowRecord, beats: Sequence[Beat]) -> str | None:
    """The beat that was actually asked. A pending beat, or one no longer in config, is not on screen."""
    known = {beat.id for beat in beats}
    if record.waiting in known and record.beats.get(record.waiting) == SHOWN:
        return record.waiting
    return next((beat.id for beat in beats if record.beats.get(beat.id) == SHOWN), None)


def is_active(record: FlowRecord) -> bool:
    return record.phase == ONBOARDING


def apply(
    record: FlowRecord,
    event: Event,
    beats: Sequence[Beat],
    facts: Mapping[str, object],
    *,
    now: str,
) -> Step:
    """The record after ``event``. The input record is not modified."""
    working = align(record, beats, facts)
    if not is_active(working):
        return _ended(working, event)
    step = _dispatch(working, event, beats, facts)
    if step.record != working:
        step = Step(step.record.model_copy(update={"updated_at": now}), step.result, step.reminder)
    return step


def _dispatch(record: FlowRecord, event: Event, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    if isinstance(event, Shown):
        return _shown(record, event, beats, facts)
    if isinstance(event, Answered):
        return _answered(record, event, beats, facts)
    if isinstance(event, Skipped):
        return _skipped(record, event, beats, facts)
    if isinstance(event, Wrote):
        return _wrote(record, event, beats, facts)
    if isinstance(event, End):
        return _end(record, event, beats, facts)
    if isinstance(event, Done):
        return _done(record, event, beats, facts)
    raise TypeError(f"unknown onboarding event {type(event).__name__}")


def _ended(record: FlowRecord, event: Event) -> Step:
    if isinstance(event, Done) and record.phase == COMPLETE:
        return Step(record, StepView(completed=True), "complete")
    if isinstance(event, End):
        return Step(record, StepView(skipped=True))
    return Step(record, StepView(error=_ENDED))


def _index(beats: Sequence[Beat]) -> dict[str, Beat]:
    return {beat.id: beat for beat in beats}


def _any_pending(statuses: Mapping[str, str], beats: Sequence[Beat]) -> bool:
    return any(statuses.get(beat.id) == PENDING for beat in beats)


def _statuses(record: FlowRecord) -> dict[str, str]:
    return dict(record.beats)


def _replace(
    record: FlowRecord,
    statuses: dict[str, str],
    beats: Sequence[Beat],
    *,
    picks: dict[str, str | None] | None = None,
) -> FlowRecord:
    """Close the ritual once none of the configured beats are still pending."""
    phase, ended = record.phase, record.ended_by
    if phase == ONBOARDING and not _any_pending(statuses, beats):
        phase, ended = COMPLETE, ended or "done"
    update: dict[str, object] = {"beats": statuses, "phase": phase, "ended_by": ended, "waiting": None}
    if picks is not None:
        update["picks"] = picks
    return record.model_copy(update=update)


def _known(beats: Sequence[Beat], beat_id: str) -> StepView | None:
    if beat_id in _index(beats):
        return None
    return StepView(error=f"unknown beat {beat_id}")


def _shown(record: FlowRecord, event: Shown, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    if err := _known(beats, event.beat):
        return Step(record, err)
    spec = _index(beats)[event.beat]
    statuses = _statuses(record)
    if not holds(spec.when, facts):
        statuses[event.beat] = SKIPPED
        return _moved(record, statuses, beats, event.beat, skipped=True)
    if statuses.get(event.beat) in {DONE, SKIPPED}:
        return _settled(record, beats, event.beat)
    for earlier in beats:
        if earlier.id == event.beat:
            break
        if statuses.get(earlier.id) == SHOWN:
            statuses[earlier.id] = DONE
    statuses[event.beat] = SHOWN
    updated = _replace(record, statuses, beats)
    if updated.phase == ONBOARDING:
        updated = updated.model_copy(update={"waiting": event.beat})
    return Step(updated, _view(updated, beats, beat=event.beat), f"after_{event.beat}")


def _answered(record: FlowRecord, event: Answered, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    del facts
    if err := _known(beats, event.beat):
        return Step(record, err)
    if not event.note.strip():
        return Step(record, StepView(beat=event.beat, error="an answer needs what the user said"))
    if record.beats.get(event.beat) in {DONE, SKIPPED}:
        return Step(record, StepView(beat=event.beat, error=f"beat {event.beat} is already settled"))
    if record.beats.get(event.beat) != SHOWN:
        return Step(record, StepView(beat=event.beat, error="that beat has not been asked yet"))
    statuses = _statuses(record)
    statuses[event.beat] = DONE
    picks = dict(record.picks)
    picks[event.beat] = event.note.strip()
    updated = _replace(record, statuses, beats, picks=picks)
    return Step(updated, _view(updated, beats, beat=event.beat), f"after_{event.beat}")


def _skipped(record: FlowRecord, event: Skipped, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    del facts
    if err := _known(beats, event.beat):
        return Step(record, err)
    if record.beats.get(event.beat) in {DONE, SKIPPED}:
        return Step(record, StepView(beat=event.beat, error=f"beat {event.beat} is already settled"))
    statuses = _statuses(record)
    statuses[event.beat] = SKIPPED
    return _moved(record, statuses, beats, event.beat, skipped=True)


def _wrote(record: FlowRecord, event: Wrote, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    del facts
    if err := _known(beats, event.beat):
        return Step(record, err)
    updated = record.model_copy(update={"waiting": None})
    return Step(updated, StepView(beat=event.beat, wrote_instead=True, next_beat=next_pending(updated, beats)), "wrote")


def _end(record: FlowRecord, event: End, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    del facts
    statuses = _statuses(record)
    for beat in beats:
        if statuses.get(beat.id) != DONE:
            statuses[beat.id] = SKIPPED
    updated = record.model_copy(
        update={"phase": PHASE_SKIPPED, "ended_by": event.outcome, "waiting": None, "beats": statuses}
    )
    return Step(updated, StepView(skipped=True, completed=False), "skip_all")


def _done(record: FlowRecord, event: Done, beats: Sequence[Beat], facts: Mapping[str, object]) -> Step:
    del event, facts
    pending = next_pending(record, beats)
    if pending is not None:
        return Step(record, StepView(error="beats are still pending; skip them or answer them", next_beat=pending))
    statuses = _statuses(record)
    for beat in beats:
        if statuses.get(beat.id) == SHOWN:
            statuses[beat.id] = DONE
    updated = record.model_copy(update={"phase": COMPLETE, "ended_by": "done", "waiting": None, "beats": statuses})
    return Step(updated, StepView(completed=True), "complete")


def _view(record: FlowRecord, beats: Sequence[Beat], *, beat: str | None, skipped: bool = False) -> StepView:
    return StepView(
        beat=beat,
        skipped=skipped,
        next_beat=next_pending(record, beats),
        completed=record.phase == COMPLETE,
    )


def _moved(record: FlowRecord, statuses: dict[str, str], beats: Sequence[Beat], beat: str, *, skipped: bool) -> Step:
    updated = _replace(record, statuses, beats)
    return Step(updated, _view(updated, beats, beat=beat, skipped=skipped), "moved_on")


def _settled(record: FlowRecord, beats: Sequence[Beat], beat: str) -> Step:
    view = StepView(beat=beat, error=f"beat {beat} is already settled", next_beat=next_pending(record, beats))
    return Step(record, view)


def _atom(atom: Atom, facts: Mapping[str, object]) -> bool:
    if atom.op not in _OPS or atom.fact not in facts:
        return False
    value = facts[atom.fact]
    if atom.op == "truthy":
        return bool(value)
    if atom.op == "falsy":
        return not bool(value)
    if atom.op == "eq":
        return value == atom.value
    if atom.op == "ne":
        return value != atom.value
    left, right = _num(value), _num(atom.value)
    if left is None or right is None:
        return False
    if atom.op == "gt":
        return left > right
    if atom.op == "gte":
        return left >= right
    if atom.op == "lt":
        return left < right
    return left <= right


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
