"""A model-call-limit message must not reach the user."""

from nwb.agent.runner import ABSTAIN_UNCONFIRMED, _TokenGate, friendly, public_answer


def test_limit_message_becomes_an_abstention() -> None:
    raw = "Model call limits exceeded: run limit (8/8)"
    out = public_answer(raw)
    assert out == ABSTAIN_UNCONFIRMED
    assert "8/8" not in out
    assert "exceeded" not in out.lower()
    assert "not on record" in out


def test_ordinary_answer_is_unchanged() -> None:
    assert public_answer("18% [DEC-2026-005].") == "18% [DEC-2026-005]."


def test_streamed_limit_phrase_is_not_a_token() -> None:
    """Earlier chunks of the limit message must not already have been sent."""
    gate = _TokenGate()
    sent = [gate.push(piece) for piece in ("Model call ", "limits exceeded: ", "run limit (8/8)")]
    assert gate.flush() == ""
    shown = "".join(sent)
    assert "Model call" not in shown
    assert "exceeded" not in shown.lower()
    assert "8/8" not in shown
    assert gate.blocked
    assert gate.buffer == ABSTAIN_UNCONFIRMED


def test_ordinary_tokens_still_stream() -> None:
    gate = _TokenGate()
    sent = gate.push("18% [DEC-2026-005].") + gate.flush()
    assert sent == "18% [DEC-2026-005]."
    assert not gate.blocked


def test_a_false_start_is_released() -> None:
    gate = _TokenGate()
    held = gate.push("Model ")
    rest = gate.push("of the policy is 3 days.")
    assert "limits exceeded" not in (held + rest).lower()
    assert (held + rest + gate.flush()) == "Model of the policy is 3 days."


def test_friendly_hides_a_limit_exception() -> None:
    class ModelCallLimitExceeded(Exception):
        pass

    text = friendly(ModelCallLimitExceeded("run limit (8/8)"))
    assert "8/8" not in text
    assert "not on record" in text
