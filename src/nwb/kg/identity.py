"""Decide link, suggestion or separate from config. ``finalize()`` finds the pairs; this does not merge.

The numbers and the name fold come from the deleted ``resolver`` (exact id or email links; a shared
name does not merge; one initial-plus-surname scores higher than several; external stays apart from
internal, even when an email or id matches). A pair at or above ``suggestion_threshold`` is a suggestion.
Nothing here deletes a node.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nwb.conf.models import IdentityRule, NameNorm, ResolutionConfig

_DASH = " — "


class Verdict(StrEnum):
    LINK = "link"
    SUGGEST = "suggest"
    SEPARATE = "separate"


@dataclass(frozen=True, slots=True)
class Record:
    """One entity read back after dedup. ``values`` are already normalised logical fields."""

    id: str
    label: str
    name: str
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class IdentityLink:
    left_id: str
    right_id: str
    field: str


@dataclass(frozen=True, slots=True)
class Suggestion:
    left_id: str
    right_id: str
    label: str
    left_name: str
    right_name: str
    reason: str
    score: float


@dataclass(frozen=True, slots=True)
class ParsedDuplicate:
    label: str
    name_a: str
    name_b: str
    reason: str


def normalise_name(name: str, cfg: NameNorm) -> str:
    """Fold a display name the way the old resolver did, using the patterns in config."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode() if cfg.ascii_fold else name
    text = text.lower()
    if cfg.drop_parenthetical:
        text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(cfg.strip_pattern, " ", text)
    return " ".join(text.split())


def normalise_value(raw: Any, mode: str, name_cfg: NameNorm) -> str:
    text = ("true" if raw else "false") if isinstance(raw, bool) else str(raw).strip()
    if not text:
        return ""
    if mode == "lower":
        return text.lower()
    if mode == "name":
        return normalise_name(text, name_cfg)
    return text


def read_property(props: dict[str, Any], logical: str) -> Any:
    """Read ``logical`` or the one signed ``*__logical`` column. Two signed values are a conflict: skip."""
    direct = props.get(logical)
    if direct not in (None, ""):
        return direct
    suffix = f"__{logical}"
    found = [value for key, value in props.items() if key.endswith(suffix) and value not in (None, "")]
    if len(found) == 1:
        return found[0]
    return None


def record_values(props: dict[str, Any], rule: IdentityRule, name_cfg: NameNorm) -> dict[str, str]:
    values: dict[str, str] = {}
    for spec in (*rule.link_by, *rule.keep_apart):
        raw = read_property(props, spec.property)
        if raw is None:
            continue
        text = normalise_value(raw, spec.normalise, name_cfg)
        if text:
            values[spec.field] = text
    return values


def link_groups(nodes: list[Record], cfg: ResolutionConfig) -> list[IdentityLink]:
    """Pairs that share an email or id. The nodes stay; the caller writes a link edge."""
    by_label: dict[str, list[Record]] = {}
    for node in nodes:
        by_label.setdefault(node.label, []).append(node)
    found: list[IdentityLink] = []
    seen: set[tuple[str, str, str]] = set()
    for label, group in by_label.items():
        rule = cfg.rule_for(label)
        if rule is None:
            continue
        buckets: dict[tuple[str, str], list[str]] = {}
        for node in group:
            for spec in rule.link_by:
                value = node.values.get(spec.field)
                if not value:
                    continue
                buckets.setdefault((spec.field, value), []).append(node.id)
        by_id = {node.id: node for node in group}
        for (field, _value), ids in buckets.items():
            unique = sorted(set(ids))
            for index, left in enumerate(unique):
                for right in unique[index + 1 :]:
                    key = (field, left, right)
                    if key in seen or _kept_apart(by_id[left], by_id[right], rule):
                        continue  # keep_apart wins over a shared email or id: no IDENTIFIES edge
                    seen.add(key)
                    found.append(IdentityLink(left, right, field))
    return found


def parse_probable(line: str) -> ParsedDuplicate | None:
    """Parse one ``FinalizeResult.probable_duplicates`` line. ``None`` if it is not that shape."""
    if " ~ " not in line or _DASH not in line:
        return None
    head, reason = line.split(_DASH, 1)
    label, _, rest = head.partition(" ")
    left_repr, _, right_repr = rest.partition(" ~ ")
    if not label or not left_repr or not right_repr:
        return None
    try:
        name_a = ast.literal_eval(left_repr)
        name_b = ast.literal_eval(right_repr)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(name_a, str) or not isinstance(name_b, str):
        return None
    return ParsedDuplicate(label, name_a, name_b, reason.strip())


def suggestions_for(lines: list[str], nodes: list[Record], cfg: ResolutionConfig) -> list[Suggestion]:
    """Probable duplicates at or above the threshold, after identity rules. Not merges."""
    found: list[Suggestion] = []
    seen: set[tuple[str, str, str]] = set()
    for line in lines:
        parsed = parse_probable(line)
        if parsed is None:
            continue
        group = [node for node in nodes if node.label == parsed.label]
        lefts = [node for node in group if node.name == parsed.name_a]
        rights = [node for node in group if node.name == parsed.name_b]
        if not lefts or not rights:
            continue
        score = _score(parsed, group, cfg)
        if score is None:
            continue
        for left in lefts:
            for right in rights:
                if left.id == right.id:
                    continue
                verdict = _verdict(left, right, cfg, score)
                if verdict is not Verdict.SUGGEST:
                    continue
                ordered = tuple(sorted((left, right), key=lambda node: node.id))
                key = (parsed.label, ordered[0].id, ordered[1].id)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    Suggestion(
                        ordered[0].id,
                        ordered[1].id,
                        parsed.label,
                        ordered[0].name,
                        ordered[1].name,
                        parsed.reason,
                        score,
                    )
                )
    return found


def decide(
    left: Record,
    right: Record,
    cfg: ResolutionConfig,
    *,
    reason: str | None,
    candidates: int,
) -> tuple[Verdict, float]:
    """One pair. Used by tests and by :func:`suggestions_for`."""
    score = 0.0
    if reason:
        parsed = ParsedDuplicate(left.label, left.name, right.name, reason)
        group = [left, right]
        # ``candidates`` lets a test force the initial-pool size without a graph.
        scored = _score(parsed, group, cfg, candidates=candidates)
        score = 0.0 if scored is None else scored
    return _verdict(left, right, cfg, score), score


def _verdict(left: Record, right: Record, cfg: ResolutionConfig, score: float) -> Verdict:
    rule = cfg.rule_for(left.label) if left.label == right.label else None
    if rule is not None and _kept_apart(left, right, rule):
        return Verdict.SEPARATE  # checked before any email or id agreement
    if rule is not None and _agrees(left, right, rule):
        return Verdict.LINK
    if rule is not None and _disagrees(left, right, rule):
        return Verdict.SEPARATE
    if score >= cfg.suggestion_threshold:
        return Verdict.SUGGEST
    return Verdict.SEPARATE


def _agrees(left: Record, right: Record, rule: IdentityRule) -> bool:
    for spec in rule.link_by:
        pair = _both(left, right, spec.field)
        if pair is not None and pair[0] == pair[1]:
            return True
    return False


def _disagrees(left: Record, right: Record, rule: IdentityRule) -> bool:
    for spec in rule.link_by:
        if not spec.conflict:
            continue
        pair = _both(left, right, spec.field)
        if pair is not None and pair[0] != pair[1]:
            return True
    return False


def _kept_apart(left: Record, right: Record, rule: IdentityRule) -> bool:
    """A ``keep_apart`` field that both carry and that differs (external vs internal): never the same node."""
    for spec in rule.keep_apart:
        pair = _both(left, right, spec.field)
        if pair is not None and pair[0] != pair[1]:
            return True
    return False


def _both(left: Record, right: Record, field: str) -> tuple[str, str] | None:
    a, b = left.values.get(field), right.values.get(field)
    if a and b:
        return a, b
    return None


def _score(
    parsed: ParsedDuplicate,
    group: list[Record],
    cfg: ResolutionConfig,
    *,
    candidates: int | None = None,
) -> float | None:
    key = _reason_key(parsed.reason, cfg)
    if key is None:
        return None
    if key == "initial_surname":
        pool = candidates if candidates is not None else _initial_pool(group, parsed, cfg)
        if pool > 1:
            return cfg.scores.ambiguous_initial
        return cfg.scores.initial_surname
    return cfg.scores.value(key)


def _reason_key(reason: str, cfg: ResolutionConfig) -> str | None:
    best: tuple[int, str] | None = None
    for key, text in cfg.reasons.model_dump().items():
        if text and text in reason and (best is None or len(text) > best[0]):
            best = (len(text), key)
    return None if best is None else best[1]


def _initial_pool(group: list[Record], parsed: ParsedDuplicate, cfg: ResolutionConfig) -> int:
    for name in (parsed.name_a, parsed.name_b):
        match = re.fullmatch(cfg.name.initial_pattern, normalise_name(name, cfg.name))
        if match is None:
            continue
        first, surname = match.group(1), " ".join(match.group(2).split())
        return sum(1 for node in group if _full_name_matches(node.name, first, surname, cfg.name))
    return 1


def _full_name_matches(name: str, first: str, surname: str, cfg: NameNorm) -> bool:
    """A full given name (not itself an initial) that the old initial rule would have pooled."""
    parts = normalise_name(name, cfg).split()
    if len(parts) < 2:
        return False
    given = parts[0].rstrip(".")
    if len(given) <= 1 or not given.startswith(first):
        return False
    return " ".join(parts[1:]) == surname
