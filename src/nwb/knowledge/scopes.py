"""Fact scopes: the conditions under which a fact applies, as a canonical string (``client=X;country=Y``).

Dimensions come from ``sources.yaml`` (``scope_dimensions``). A person's scope values come from the data: country
from the directory, clients from their active assignments. Resolution walks from the most specific combination to
the company-wide rule (``None``), so a contract clause beats a country rule, which beats the general policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import combinations


def format_scope(values: Mapping[str, str | None], dimensions: Sequence[str]) -> str | None:
    """``{"country": "GB", "client": "C1"}`` -> ``"client=C1;country=GB"`` (dimension order); empty -> None."""
    parts = [f"{d}={values[d]}" for d in dimensions if values.get(d)]
    extra = sorted(f"{k}={v}" for k, v in values.items() if v and k not in dimensions)
    joined = ";".join([*parts, *extra])
    return joined or None


def scope_chain(person: Mapping[str, Sequence[str]], dimensions: Sequence[str]) -> list[str | None]:
    """Every scope that can apply to this person, most specific first, ending with company-wide (``None``).

    ``person`` maps a dimension to the person's values (a person can be on several clients at once).
    """
    dims = [d for d in dimensions if person.get(d)]
    chain: list[str | None] = []
    for size in range(len(dims), 0, -1):
        for combo in combinations(dims, size):
            for values in _product([list(person[d]) for d in combo]):
                s = format_scope(dict(zip(combo, values, strict=True)), dimensions)
                if s not in chain:
                    chain.append(s)
    chain.append(None)
    return chain


def _product(lists: list[list[str]]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = [()]
    for values in lists:
        out = [(*prefix, v) for prefix in out for v in values]
    return out
