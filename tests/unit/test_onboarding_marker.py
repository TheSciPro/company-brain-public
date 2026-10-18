"""The beat marker is an id token. Instruction text inside a tag never becomes the reply."""

from __future__ import annotations

from nwb.onboarding.marker import ReplyScrubber, offered_ids, visible_reply

LEAK = "Confirm they joined the private guidance and do not save this as a memory."


def test_a_tag_with_instruction_text_is_dropped_and_is_not_an_offer() -> None:
    raw = f'Your manager is the right person.\n<onboarding-beat id="confirm_place">\n{LEAK}\n</onboarding-beat>'
    assert offered_ids(raw) == []
    shown = visible_reply(raw)
    assert shown == "Your manager is the right person."
    assert LEAK not in shown
    assert "<onboarding-beat" not in shown


def test_only_the_empty_marker_counts_and_it_is_not_shown() -> None:
    raw = 'You joined the team, and M is your manager. Does that match?\n<onboarding-beat id="confirm_place"/>'
    assert offered_ids(raw) == ["confirm_place"]
    shown = visible_reply(raw)
    assert "Does that match?" in shown
    assert "<onboarding-beat" not in shown
    assert "confirm_place" not in shown


def test_an_unclosed_tag_is_cut_through_the_end_and_does_not_count() -> None:
    raw = (
        'Your manager is the right person.\n<onboarding-beat id="confirm_place"/>\n'
        f'<onboarding-beat id="confirm_place">\n{LEAK}'
    )
    assert offered_ids(raw) == []
    shown = visible_reply(raw)
    assert shown == "Your manager is the right person."
    assert LEAK not in shown
    assert "<onboarding-beat" not in shown
    bare = f"Hello.\n<onboarding-beat {LEAK}"
    assert offered_ids(bare) == []
    assert visible_reply(bare) == "Hello."
    assert LEAK not in visible_reply(bare)


def test_streamed_instruction_text_inside_the_tag_is_never_emitted() -> None:
    scrubber = ReplyScrubber()
    chunks = [
        "Your manager is the right person.\n<onboarding-",
        'beat id="confirm_place">',
        LEAK[:20],
        LEAK[20:],
        "</onboarding-beat>",
    ]
    shown = "".join(scrubber.push(chunk) for chunk in chunks) + scrubber.finish()
    assert "Your manager is the right person." in shown
    assert LEAK not in shown
    assert "onboarding-beat" not in shown
    assert "private guidance" not in shown
