"""Country codes. Sources mix ISO 3166-1 alpha-2 with its *exceptionally reserved* codes; normalise to alpha-2.

The reservations are part of the ISO standard itself (not a fact about any one company), so they live here.
"""

from __future__ import annotations

# ISO 3166-1 exceptionally reserved code elements that stand for an assigned alpha-2 code.
_RESERVED = {"UK": "GB", "EL": "GR"}


def normalize_country(code: str | None) -> str | None:
    """'uk' -> 'GB', 'IN' -> 'IN'; empty -> None. Unknown codes pass through upper-cased."""
    if not code or not code.strip():
        return None
    c = code.strip().upper()
    return _RESERVED.get(c, c)
