"""Which classification levels may be opened as a whole graph. No person or company is special."""

from __future__ import annotations

from nwb.conf.models import ClassificationConfig, Level, TagRule
from nwb.ingest.acl import classification_config
from nwb.knowledge.levels import eligible_levels


def test_shipped_grant_and_unknown_levels_are_not_eligible() -> None:
    cfg = classification_config()
    eligible = set(eligible_levels(cfg))
    assert eligible
    for rule in cfg.tags.values():
        if rule.grants or rule.personal:
            assert rule.level not in eligible
    if cfg.unknown_tag.grants or cfg.unknown_tag.personal:
        assert cfg.unknown_tag.level not in eligible
    assert eligible <= {level.code for level in cfg.levels}


def test_a_grant_on_one_tag_closes_the_whole_level() -> None:
    cfg = ClassificationConfig(
        version=1,
        levels=(Level(code="open", rank=1, label="Open"), Level(code="mixed", rank=2, label="Mixed")),
        tags={
            "WIDE": TagRule(level="open", grants=False),
            "NARROW": TagRule(level="mixed", grants=False),
            "SECRET": TagRule(level="mixed", grants=True),
        },
        unknown_tag=TagRule(level="mixed", grants=True),
    )
    assert eligible_levels(cfg) == ("open",)


def test_unknown_tag_sink_is_closed_even_when_another_tag_looks_open() -> None:
    cfg = ClassificationConfig(
        version=1,
        levels=(Level(code="low", rank=1, label="Low"), Level(code="sink", rank=2, label="Sink")),
        tags={"WIDE": TagRule(level="sink", grants=False)},
        unknown_tag=TagRule(level="sink", grants=True),
    )
    assert eligible_levels(cfg) == ()
