"""ACL tag + acl list -> classification, share scope and per-resource grants (AGENTS §5.1).

The tag map is ``config/classification.yaml``. Unknown and missing tags use ``unknown_tag`` (fail closed).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import ClassificationConfig, TagRule
from nwb.db.models import Classification, ShareScope

_ID = re.compile(r"^\s*([\w.@-]+)")
_WINDOW = re.compile(r"(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-)?(\d{2}-\d{2})")


@lru_cache(maxsize=1)
def classification_config() -> ClassificationConfig:
    """Load the tag map from the repo ``config/`` directory. Cached; the file is the source of truth."""
    root = Path(__file__).resolve().parents[3]
    return ConfigLoader(root / "config").load("classification", ClassificationConfig)


def tag_rule(tag: str | None) -> TagRule:
    return classification_config().resolve_tag(tag)


def classify_tag(tag: str | None) -> tuple[Classification, ShareScope | None]:
    """Unknown, missing or restricted tags fail closed to the configured unknown tag."""
    rule = tag_rule(tag)
    return cast(Classification, rule.level), cast(ShareScope | None, rule.share)


def needs_grants(tag: str | None) -> bool:
    """True when a row is readable only through an explicit acl grant."""
    return tag_rule(tag).grants


def is_personal(tag: str | None) -> bool:
    """True when the tag marks owner-only personal data."""
    return tag_rule(tag).personal


@dataclass(frozen=True, slots=True)
class GrantSpec:
    subject_type: Literal["person", "group"]
    subject_id: str
    valid_from: date | None = None
    valid_to: date | None = None


def parse_acl(entries: list[str] | str | None, groups: Collection[str], people: Collection[str]) -> list[GrantSpec]:
    """``["<group id>", "<person id>", "<group id> (note, YYYY-MM-DD..MM-DD)"]`` -> grant specs.

    The first token of each entry is a subject id; it must be a known group or person, otherwise the entry is
    dropped (fail closed: an unknown subject never becomes a grant). A trailing date window bounds the grant.
    """
    if entries is None:
        return []
    items = [entries] if isinstance(entries, str) else entries
    out: list[GrantSpec] = []
    for raw in items:
        m = _ID.match(str(raw))
        if not m:
            continue
        sid = m.group(1)
        start = end = None
        w = _WINDOW.search(str(raw))
        if w:
            start = date.fromisoformat(w.group(1))
            end = date.fromisoformat((w.group(2) or w.group(1)[:5]) + w.group(3))
        if sid in groups:
            out.append(GrantSpec("group", sid, start, end))
        elif sid in people:
            out.append(GrantSpec("person", sid, start, end))
    return out
