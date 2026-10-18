"""One memory system: the reader's Graphiti group, as-of, slots, audience, and the latency budget."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nwb.db.repo_sessions import SessionRepository
from nwb.errors import NotFoundError
from nwb.memory.store import rejection
from tests.agent.conftest import ASOF, Harness

WHEN = datetime(2026, 9, 28, tzinfo=UTC)


def _user_message(h: Harness, person: str, text: str) -> tuple[str, str]:
    sid = h.c.runner.start_session(person, ASOF)
    with h.c.db.unit_of_work(as_person=person) as uow:
        mid = SessionRepository(uow).add_message(sid, person, "user", text)
    return sid, mid


def test_secrets_are_rejected_and_not_stored(harness: Harness) -> None:
    receipt = harness.memory.remember_chat(
        harness.actor(),
        [("user", "my password: hunter2secret")],
        [],
        WHEN,
    )
    assert receipt.episode_id is None and receipt.rejected
    assert harness.memory.facts_as_of(harness.actor()) == []


def test_verbatim_guard_still_rejects_a_fact_that_was_not_said(harness: Harness) -> None:
    sid, mid = _user_message(harness, "NW1103", "please remember I like tea, not coffee")
    actor = harness.actor(session=sid, message=mid)
    bad = rejection(
        harness.c.write_guards(), "Likes green tea", "please remember I like tea, not coffee", verbatim=True
    )
    good = rejection(
        harness.c.write_guards(), "I like tea, not coffee", "please remember I like tea, not coffee", verbatim=True
    )
    assert bad and actor.person_id == "NW1103"
    assert good is None


def test_read_is_owner_only_and_does_not_disclose_existence(harness: Harness) -> None:
    harness.memory.append_fact(harness.actor("NW1103"), "Allergic to peanuts in the office kitchen.", None, WHEN)
    own = harness.memory.facts_as_of(harness.actor("NW1103"))
    assert own and "peanuts" in own[0].text
    with pytest.raises(NotFoundError) as other:
        harness.memory.read_fact(harness.actor("NW0201"), own[0].id)
    with pytest.raises(NotFoundError) as missing:
        harness.memory.read_fact(harness.actor("NW0201"), "does-not-exist")
    assert str(other.value) == str(missing.value) == "fact not found"


def test_recall_finds_own_fact_and_never_another_users(harness: Harness) -> None:
    harness.memory.append_fact(harness.actor("NW1103"), "Sends the Brightmart status report on Mondays.", None, WHEN)
    harness.memory.append_fact(harness.actor("NW0201"), "Brightmart invoices are due on Mondays.", None, WHEN)
    res = harness.memory.recall(harness.actor("NW1103"), "brightmart status report", {"NW1103"}, 8)
    assert res.hits and res.honesty == "ok"
    assert all("invoices" not in h.fact.text for h in res.hits)


def test_recall_is_empty_when_nothing_matches(harness: Harness) -> None:
    harness.memory.append_fact(harness.actor(), "Prefers tea with lunch on Fridays.", None, WHEN)
    res = harness.memory.recall(harness.actor(), "quarterly zebra migration", {harness.actor().person_id}, 8)
    assert res.honesty == "empty"
    assert res.hits == []
    assert "graphiti" in res.note


def test_preferences_are_not_recalled(harness: Harness) -> None:
    harness.memory.append_fact(harness.actor(), "Prefers weekly status updates in INR.", None, WHEN)
    res = harness.memory.recall(harness.actor(), "prefers weekly status INR", {harness.actor().person_id}, 8)
    assert res.hits == []


def test_recall_refuses_a_wider_audience_without_counting_hidden_facts(harness: Harness) -> None:
    harness.memory.append_fact(harness.actor(), "Sends the Brightmart status report on Mondays.", None, WHEN)
    res = harness.memory.recall(harness.actor(), "brightmart status", {"NW1103", "NW0201"}, 8)
    assert res.hits == [] and res.not_searched == ["graphiti"]
    assert "not searched" in res.note and "1" not in res.note


def test_slots_cap_each_kind(harness: Harness) -> None:
    actor = harness.actor()
    for i in range(5):
        harness.memory.append_fact(actor, f"Project atlas note number {i} is still open.", None, WHEN)
    res = harness.memory.recall(actor, "project atlas note", {actor.person_id}, 8)
    assert len(res.hits) == harness.c.memory_config.facts.slots["fact"]


def test_a_slow_recall_is_skipped_and_names_graphiti(harness: Harness) -> None:
    facts = harness.c.memory_config.facts.model_copy(update={"latency_budget_ms": 30})
    harness.memory._cfg = harness.c.memory_config.model_copy(update={"facts": facts})
    harness.memory.recall_delay_s = 0.4
    harness.memory.append_fact(harness.actor(), "Sends the Brightmart status report on Mondays.", None, WHEN)
    res = harness.memory.recall(harness.actor(), "brightmart status", {"NW1103"}, 8)
    assert res.skipped and res.hits == [] and res.not_searched == ["graphiti"]
    assert "not searched: graphiti" in res.note


VENTING = "ugh this is ridiculous, I hate this"
SELF_CORRECTION = "wait, no scratch that, I meant the other file"
STACK = 'Traceback (most recent call last):\n  File "worker.py", line 42, in run\nRuntimeError: boom'
DUMP = (
    "ERROR connection failed errno 111 while the worker was still starting up in the pool\n"
    "FATAL worker exception: panic in the pool after the socket dropped\n"
    "ERROR retry failed errno 111 and the upstream exception was not recovered\n"
    "the FATAL panic left the job failed and the exception was logged again\n"
)


@pytest.mark.parametrize("noisy", [VENTING, SELF_CORRECTION, STACK, DUMP])
def test_quality_gate_rejects_noise_before_a_fact_is_stored(harness: Harness, noisy: str) -> None:
    reason = rejection(harness.c.write_guards(), noisy, None)
    assert reason and "do not retry" in reason.lower()
    receipt = harness.memory.append_fact(harness.actor(), noisy, None, WHEN)
    assert receipt.episode_id is None and receipt.rejected
    assert harness.memory.facts_as_of(harness.actor()) == []


def test_a_durable_fact_still_passes_the_quality_gate(harness: Harness) -> None:
    text = "Sends the weekly status report on Mondays."
    assert rejection(harness.c.write_guards(), text, None) is None
    receipt = harness.memory.append_fact(harness.actor(), text, None, WHEN)
    assert receipt.episode_id and harness.memory.facts_as_of(harness.actor())
