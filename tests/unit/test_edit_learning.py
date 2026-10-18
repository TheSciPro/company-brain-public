"""An edited approval becomes a dated Prefer directive; a later edit supersedes it in place."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from nwb.agent.context import RuntimeContext
from nwb.conf import ConfigLoader, MemoryConfig
from nwb.memory.directives import Directive, apply_directive
from nwb.memory.edit_learning import (
    DirectiveRejectedError,
    EditLearning,
    FieldChange,
    changed_spans,
    diff_args,
    fallback_phrase,
)
from nwb.memory.guards import MemoryWriteGate, QualityGate, SecretGuard
from nwb.memory.phrasing import (
    LLMPhraser,
    PhraseRecorder,
    PhraseRequest,
    RecordingMissError,
    canonical,
    keeps_concrete_change,
)
from nwb.prompts import PromptLibrary
from tests.conftest import ROOT

FIXTURES = ROOT / "tests" / "fixtures" / "llm_calls" / "directive_phrase"
ASOF = "2026-09-28"


def _cfg() -> MemoryConfig:
    return ConfigLoader(ROOT / "config").load("memory", MemoryConfig)


def test_diff_is_deterministic_and_ignores_unchanged_and_configured_fields() -> None:
    before = {"kind": "send_reminder", "title": "Review", "message": "Please review", "note": "internal"}
    after = {"kind": "send_reminder", "title": "Review", "message": "Please review by Friday.", "note": "changed"}
    changes = diff_args(before, after, frozenset({"note"}))
    assert changes == [FieldChange("message", "Please review", "Please review by Friday.")]
    assert diff_args(before, before, frozenset()) == []


def test_directive_supersedes_the_same_scope_in_place() -> None:
    cfg = _cfg().edit_learning
    seeded = (ROOT / "agent" / "templates" / "USER.md").read_text(encoding="utf-8")
    first = Directive("Prefer", "Keep reminders to one short paragraph.", ASOF, "active", "send_reminder.message")
    second = Directive(
        "Prefer", "Keep reminders to a single sentence.", "2026-10-02", "active", "send_reminder.message"
    )
    other = Directive("Always", "Use the reader's currency.", ASOF, "active", "send_reminder.title")
    text = apply_directive(apply_directive(apply_directive(seeded, first, cfg), other, cfg), second, cfg)
    assert "status: superseded | scope: send_reminder.message" in text
    assert "status: active | scope: send_reminder.message" in text
    assert text.index("superseded | scope: send_reminder.message") < text.index("active | scope: send_reminder.message")
    assert "status: active | scope: send_reminder.title" in text
    assert "(none yet)" not in text
    assert apply_directive(text, second, cfg) == text


def test_supersede_changes_only_the_comment_when_the_text_repeats_the_status() -> None:
    cfg = _cfg().edit_learning
    seeded = (ROOT / "agent" / "templates" / "USER.md").read_text(encoding="utf-8")
    words = f"Mark tickets status: {cfg.active_status} only after triage."
    first = Directive("Prefer", words, ASOF, cfg.active_status, "update_ticket.status")
    second = Directive(
        "Prefer", "Leave the ticket status alone.", "2026-10-02", cfg.active_status, "update_ticket.status"
    )
    text = apply_directive(apply_directive(seeded, first, cfg), second, cfg)
    assert f"- Prefer: {words}\n" in text  # the directive's own words are untouched
    old = f"status: {cfg.superseded_status} | scope: update_ticket.status -->"
    new = f"status: {cfg.active_status} | scope: update_ticket.status -->"
    assert text.count(old) == 1 and text.count(new) == 1  # one superseded, one active, no duplicate
    assert text.index(old) < text.index("- Prefer: Leave the ticket status alone.")
    assert apply_directive(text, second, cfg) == text


def test_fallback_phrase_uses_only_the_diff() -> None:
    phrase = fallback_phrase("send_reminder", FieldChange("message", "a", "b"), 240)
    assert "message" in phrase and "b" in phrase and not phrase.startswith("Prefer")


class _Boom(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "boom"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, **kwargs: object) -> ChatResult:
        raise AssertionError("replay must not call the model")


def test_recorded_phrase_is_replayed_without_calling_the_model() -> None:
    cfg = _cfg().edit_learning
    before, after = changed_spans("Please review", "Please review by Friday, in one short paragraph.")
    request = PhraseRequest(
        tool="propose_action",
        kind="send_reminder",
        field="message",
        before=before,
        after=after,
    )
    phraser = LLMPhraser(_Boom(), PromptLibrary(ROOT / "prompts"), cfg, PhraseRecorder(FIXTURES, "replay"))
    text = phraser.phrase(request)
    assert text == "Ask for the review by Friday and keep reminder messages to one short paragraph."
    with pytest.raises(RecordingMissError):
        phraser.phrase(PhraseRequest("propose_action", "send_reminder", "title", "a", "b"))


def test_directive_prompt_keeps_concrete_changes() -> None:
    """The learned line keeps the words the approver added. It does not generalise them away."""
    text = (ROOT / "prompts" / "directive_phrase.md").read_text(encoding="utf-8")
    assert "Do not generalise" in text
    assert "concrete word" in text
    before, after = "Please review", "Please review by Friday, in one short paragraph."
    vague = "Please review the card in one short paragraph."
    assert not keeps_concrete_change(before, after, vague)
    assert keeps_concrete_change(before, after, "Ask for the review by Friday, in one short paragraph.")


class _Vague(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "vague"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, **kwargs: object) -> ChatResult:
        sentence = '{"text": "Please review the card in one short paragraph."}'
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=sentence))])


def test_a_phrase_that_drops_an_added_word_is_discarded() -> None:
    cfg = _cfg().edit_learning
    request = PhraseRequest(
        tool="propose_action",
        kind="send_reminder",
        field="message",
        before="Please review",
        after="Please review by Friday, in one short paragraph.",
    )
    assert LLMPhraser(_Vague(), PromptLibrary(ROOT / "prompts"), cfg).phrase(request) is None
    assert "Friday" in fallback_phrase("send_reminder", FieldChange("message", request.before, request.after), 240)


def test_fixture_request_matches_the_canonical_diff() -> None:
    before, after = changed_spans("Please review", "Please review by Friday, in one short paragraph.")
    payload = {
        "tool": "propose_action",
        "kind": "send_reminder",
        "field": "message",
        "before": before,
        "after": after,
    }
    found = False
    for path in Path(FIXTURES).glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if canonical(doc["request"]) == canonical(payload):
            found = True
            assert "Friday" in doc["response"]
    assert found


class _Files:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self, company_id: str, user_id: str, name: str) -> str:
        return self.text

    def write(self, company_id: str, user_id: str, name: str, content: str) -> None:
        self.text = content


class _Says:
    def __init__(self, text: str) -> None:
        self.text = text
        self.seen: PhraseRequest | None = None

    def phrase(self, request: PhraseRequest) -> str | None:
        self.seen = request
        return self.text


def _gate() -> MemoryWriteGate:
    mc = _cfg()
    return MemoryWriteGate(SecretGuard(mc.secret_patterns), QualityGate(mc.quality))


def test_changed_spans_are_whole_words_without_the_shared_prefix() -> None:
    old, new = changed_spans(
        "Please review by Friday, in one short paragraph.",
        "Please review by Monday, in a single sentence.",
    )
    assert old == "Friday, one short paragraph."
    assert new == "Monday, a single sentence."


def test_a_seeded_profile_full_of_error_words_still_stores_a_clean_line() -> None:
    """error_dump trips on a long profile. That must not drop a new Prefer line."""
    noise = (
        "The export failed. The import failed. ERROR from the worker. "
        "FATAL panic in the batch. exception while saving. " + ("working context " * 20)
    )
    assert len(noise) >= 160
    files = _Files(noise)
    phrase = "Ask for the review by Friday."
    learning = EditLearning(files, _Says(phrase), _cfg().edit_learning, _gate())
    drafts = [{"name": "propose_action", "args": {"kind": "send_reminder", "message": "Please review"}}]
    decisions = [
        {
            "type": "edit",
            "edited_action": {
                "name": "propose_action",
                "args": {"kind": "send_reminder", "message": "Please review by Friday."},
            },
        }
    ]
    receipts = learning.learn(RuntimeContext("co", "u", "s", "r", ASOF, "m"), drafts, decisions)
    assert receipts == [{"event": "DIRECTIVE", "scope": "send_reminder.message", "path": "USER.md"}]
    assert f"Prefer: {phrase}" in files.text
    assert "FATAL panic" in files.text


def test_a_rejected_candidate_line_is_raised_and_not_written() -> None:
    original = "## Directives\n- (none yet)\n"
    files = _Files(original)
    refused = "ugh I hate this Friday"
    learning = EditLearning(files, _Says(refused), _cfg().edit_learning, _gate())
    drafts = [{"name": "propose_action", "args": {"kind": "send_reminder", "message": "Please review"}}]
    decisions = [
        {
            "type": "edit",
            "edited_action": {
                "name": "propose_action",
                "args": {"kind": "send_reminder", "message": "Please review Friday"},
            },
        }
    ]
    with pytest.raises(DirectiveRejectedError, match=r"rejected \(venting\)") as caught:
        learning.learn(RuntimeContext("co", "u", "s", "r", ASOF, "m"), drafts, decisions)
    assert refused not in str(caught.value)
    assert caught.value.receipts[0]["scope"] == "send_reminder.message"
    assert files.text == original


def test_a_mixed_edit_returns_the_stored_line_and_the_refusal() -> None:
    """One refused field must not hide a field that was stored, and must not raise."""
    files = _Files("## Directives\n- (none yet)\n")

    class _ByField:
        def phrase(self, request: PhraseRequest) -> str:
            if request.field == "message":
                return "ugh I hate this Friday"
            return "Put Status in the title."

    learning = EditLearning(files, _ByField(), _cfg().edit_learning, _gate())
    drafts = [
        {
            "name": "propose_action",
            "args": {"kind": "send_reminder", "message": "Please review", "title": "Review"},
        }
    ]
    decisions = [
        {
            "type": "edit",
            "edited_action": {
                "name": "propose_action",
                "args": {"kind": "send_reminder", "message": "Please review Friday", "title": "Status"},
            },
        }
    ]
    receipts = learning.learn(RuntimeContext("co", "u", "s", "r", ASOF, "m"), drafts, decisions)
    assert [item["event"] for item in receipts] == ["REJECTED", "DIRECTIVE"]
    assert receipts[0]["scope"] == "send_reminder.message"
    assert "rejected (venting)" in receipts[0]["reason"]
    assert "ugh" not in receipts[0]["reason"]
    assert receipts[1]["scope"] == "send_reminder.title"
    assert "Put Status in the title." in files.text
    assert "ugh" not in files.text


def test_a_suffix_past_the_excerpt_window_is_phrased_from_the_changed_words() -> None:
    shared = "word " * 120
    before, after = shared + "old", shared + "TAILWORD"
    assert len(after) > _cfg().edit_learning.excerpt_chars
    phraser = _Says("Keep the wording brief.")
    learning = EditLearning(_Files(""), phraser, _cfg().edit_learning, _gate())
    phrase = learning._phrase("propose_action", "send_reminder", FieldChange("message", before, after))
    assert phraser.seen is not None and phraser.seen.after == "TAILWORD"
    assert "TAILWORD" in phrase
    assert "brief" not in phrase


def test_fallback_phrase_keeps_a_word_past_half_the_budget() -> None:
    phrase = fallback_phrase("send_reminder", FieldChange("message", "x", ("w" * 130) + "TAILWORD"), 240)
    assert "TAILWORD" in phrase and len(phrase) <= 240
    longer = fallback_phrase("send_reminder", FieldChange("message", "x", ("w" * 400) + "TAILWORD"), 240)
    assert "TAILWORD" in longer and len(longer) <= 240
