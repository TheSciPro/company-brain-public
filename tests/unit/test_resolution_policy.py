"""Resolution policy, with no graph and no model.

Two people who share a display name and not an email stay two people. A near-duplicate
organisation is a suggestion at the configured score, never a merge.
"""

from __future__ import annotations

from pathlib import Path

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import ResolutionConfig
from nwb.kg.identity import (
    IdentityLink,
    Record,
    Verdict,
    decide,
    link_groups,
    normalise_name,
    suggestions_for,
)

ROOT = Path(__file__).resolve().parents[2]
_SUFFIX = "the same name once legal suffixes and abbreviations are normalised"
_INITIAL = "the same surname, with one given name written as an initial"


def _cfg() -> ResolutionConfig:
    return ConfigLoader(ROOT / "config").load("resolution", ResolutionConfig)


def _line(label: str, left: str, right: str, reason: str) -> str:
    return f"{label} {left!r} ~ {right!r} — {reason}"


def test_shipped_scores_are_the_old_resolver_numbers() -> None:
    cfg = _cfg()
    assert cfg.suggestion_threshold == 0.9
    assert cfg.scores.exact_link == 1.0
    assert cfg.scores.normalised_suffix == 0.95
    assert cfg.scores.initial_surname == 0.9
    assert cfg.scores.reordered_words == 0.85
    assert cfg.scores.shared_name == 0.5
    assert cfg.scores.ambiguous_initial == 0.4


def test_same_name_different_emails_stay_separate() -> None:
    cfg = _cfg()
    left = Record("p-1", "Person", "Ada Lovelace", {"email": "ada@example.com", "id": "p-1"})
    right = Record("p-2", "Person", "Ada Lovelace", {"email": "ada.other@example.com", "id": "p-2"})
    verdict, score = decide(left, right, cfg, reason=_SUFFIX, candidates=1)
    assert verdict is Verdict.SEPARATE
    assert score == cfg.scores.normalised_suffix
    assert link_groups([left, right], cfg) == []
    line = _line("Person", "Ada Lovelace", "Ada Lovelace", _SUFFIX)
    assert suggestions_for([line], [left, right], cfg) == []


def test_same_email_is_a_link_and_not_a_suggestion() -> None:
    cfg = _cfg()
    left = Record("p-1", "Person", "Ada Lovelace", {"email": "ada@example.com", "id": "p-1"})
    right = Record("p-3", "Person", "Augusta Ada", {"email": "ada@example.com", "id": "p-3"})
    assert link_groups([left, right], cfg) == [IdentityLink("p-1", "p-3", "email")]
    line = _line("Person", "Ada Lovelace", "Augusta Ada", _SUFFIX)
    assert suggestions_for([line], [left, right], cfg) == []
    verdict, _score = decide(left, right, cfg, reason=_SUFFIX, candidates=1)
    assert verdict is Verdict.LINK


def test_near_duplicate_org_is_a_suggestion_and_not_a_link() -> None:
    cfg = _cfg()
    left = Record("o-1", "Organization", "Acme Company", {})
    right = Record("o-2", "Organization", "Acme", {})
    line = _line("Organization", "Acme Company", "Acme", _SUFFIX)
    found = suggestions_for([line], [left, right], cfg)
    assert len(found) == 1
    assert found[0].score == cfg.scores.normalised_suffix
    assert found[0].score >= cfg.suggestion_threshold
    assert {found[0].left_name, found[0].right_name} == {"Acme Company", "Acme"}
    assert link_groups([left, right], cfg) == []


def test_ambiguous_initial_is_not_suggested() -> None:
    cfg = _cfg()
    mina = Record("1", "Person", "Mina Okonkwo", {})
    mira = Record("2", "Person", "Mira Okonkwo", {})
    initial = Record("3", "Person", "M. Okonkwo", {})
    line = _line("Person", "M. Okonkwo", "Mina Okonkwo", _INITIAL)
    assert suggestions_for([line], [mina, mira, initial], cfg) == []
    verdict, score = decide(initial, mina, cfg, reason=_INITIAL, candidates=2)
    assert score == cfg.scores.ambiguous_initial
    assert verdict is Verdict.SEPARATE


def test_one_full_name_is_suggested_and_not_linked() -> None:
    cfg = _cfg()
    mina = Record("1", "Person", "Mina Okonkwo", {})
    initial = Record("3", "Person", "M. Okonkwo", {})
    line = _line("Person", "M. Okonkwo", "Mina Okonkwo", _INITIAL)
    found = suggestions_for([line], [mina, initial], cfg)
    assert len(found) == 1
    assert found[0].score == cfg.scores.initial_surname
    verdict, score = decide(initial, mina, cfg, reason=_INITIAL, candidates=1)
    assert verdict is Verdict.SUGGEST
    assert score == 0.9
    assert link_groups([mina, initial], cfg) == []


def test_external_and_internal_stay_apart_even_when_email_or_id_agrees() -> None:
    cfg = _cfg()
    outside = Record("a", "Person", "Ada Lovelace", {"external": "true"})
    inside = Record("b", "Person", "Ada Lovelace", {"external": "false"})
    verdict, _score = decide(outside, inside, cfg, reason=_SUFFIX, candidates=1)
    assert verdict is Verdict.SEPARATE
    shared = {"email": "ada@example.com", "id": "NW0042"}
    same_out = Record("a", "Person", "Ada Lovelace", {**shared, "external": "true"})
    same_in = Record("b", "Person", "A. Lovelace", {**shared, "external": "false"})
    agreed, _score = decide(same_out, same_in, cfg, reason=_INITIAL, candidates=1)
    assert agreed is Verdict.SEPARATE  # keep_apart is checked before the shared email and id
    assert link_groups([same_out, same_in], cfg) == []  # and no IDENTIFIES edge is written
    both_in = Record("c", "Person", "Ada L.", {**shared, "external": "false"})
    assert decide(same_in, both_in, cfg, reason=None, candidates=1)[0] is Verdict.LINK
    assert {link.field for link in link_groups([same_out, same_in, both_in], cfg)} == {"email", "id"}
    assert all("a" not in (link.left_id, link.right_id) for link in link_groups([same_out, same_in, both_in], cfg))


def test_normalise_name_folds_the_old_way() -> None:
    cfg = _cfg()
    assert normalise_name("  Ada   LOVELACE (locum) ", cfg.name) == "ada lovelace"
