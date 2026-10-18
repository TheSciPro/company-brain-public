"""Precedence rules in isolation (synthetic facts, embedded FalkorDB)."""

from datetime import date
from pathlib import Path

import pytest

from nwb.temporal.facts import Authority, Fact, normalise_value
from nwb.temporal.graph_facts import GraphFactBook
from nwb.temporal.precedence import PrecedenceEngine, QuoteMismatchError, check_quote


def fact(value, start, auth, src, scope=None, draft=False):
    return Fact(
        key="k",
        about="A",
        value=value,
        valid_from=date.fromisoformat(start),
        authority=Authority(auth),
        src_type="document",
        src_id=src,
        quote="q",
        scope=scope,
        draft=draft,
    )


@pytest.fixture
def engine(tmp_path: Path):
    book = GraphFactBook.open_lite(tmp_path / "facts.falkor", company="NW", levels=("internal",))
    try:
        yield PrecedenceEngine(book)
    finally:
        book.release()


def test_lower_authority_never_overwrites_and_raises_alert(engine):
    engine.on_new_fact(fact("5", "2026-01-01", 5, "contract"), "internal")
    newer = engine.on_new_fact(fact("9", "2026-03-01", 1, "chat"), "internal")
    assert newer.status == "conflict"
    ans = engine.as_of("k", "A", date(2026, 6, 1))
    assert ans.fact is not None and ans.fact.value == "5"
    assert [c.src_id for c in ans.contested] == ["chat"]
    assert engine._book.alert_count() == 1


def test_same_value_corroborates_and_replay_is_idempotent(engine):
    first = engine.on_new_fact(fact("18", "2026-02-05", 2, "dec"), "internal")
    second = engine.on_new_fact(fact("18", "2026-02-10", 4, "pex"), "internal")
    assert (first.status, second.status, second.related_id) == ("current", "corroborates", first.id)
    assert engine.on_new_fact(fact("18", "2026-02-10", 4, "pex"), "internal").id == second.id


def test_higher_authority_closes_the_old_fact_and_keeps_both_clocks(engine):
    old = engine.on_new_fact(fact("2", "2026-01-01", 3, "v2"), "internal")
    new = engine.on_new_fact(fact("3", "2026-04-01", 3, "v3"), "internal")
    assert new.status == "current" and new.supersedes_id == old.id
    assert new.recorded_at is not None
    closed = engine._book.get(old.id)
    assert closed is not None
    assert closed.status == "superseded"
    assert closed.valid_to == date(2026, 3, 31)
    assert closed.recorded_at is not None
    earlier = engine.as_of("k", "A", date(2026, 2, 1))
    assert earlier.fact is not None and earlier.fact.value == "2"
    assert earlier.fact.valid_to is not None
    later = engine.as_of("k", "A", date(2026, 6, 1))
    assert later.fact is not None and later.fact.value == "3"
    assert later.replaced is not None and later.replaced.value == "2"


def test_supersession_saves_the_new_fact_before_closing_the_old(engine, monkeypatch):
    """A failed close leaves the newer fact current (never nothing current), and a replay finishes the close."""
    old = engine.on_new_fact(fact("2", "2026-01-01", 3, "v2"), "internal")
    book = engine._book
    real_close = book.close

    def broken_close(*_args, **_kwargs):
        raise ConnectionError("falkordb went away")

    monkeypatch.setattr(book, "close", broken_close)
    with pytest.raises(ConnectionError):
        engine.on_new_fact(fact("3", "2026-04-01", 3, "v3"), "internal")
    later = engine.as_of("k", "A", date(2026, 6, 1))
    assert later.fact is not None and later.fact.value == "3"
    stale = book.get(old.id)
    assert stale is not None and stale.status == "current"

    monkeypatch.setattr(book, "close", real_close)
    replay = engine.on_new_fact(fact("3", "2026-04-01", 3, "v3"), "internal")
    assert replay.supersedes_id == old.id
    healed = book.get(old.id)
    assert healed is not None and healed.status == "superseded" and healed.valid_to == date(2026, 3, 31)


def test_normalise_value():
    assert normalise_value(18.0) == normalise_value("18") == "18"


def test_quote_guard_whitespace_normalised():
    check_quote("within 24\nhours", "notify  within 24 hours of becoming aware")
    with pytest.raises(QuoteMismatchError):
        check_quote("within 48 hours", "notify within 24 hours")
