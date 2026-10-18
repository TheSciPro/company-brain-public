"""Guard: nothing data-specific may live in code, config, prompts or agent assets (audit Part E.1).

The forbidden list is *derived from the data* (people, documents, clients, groups, offices, record ids) plus the
audit's demo-topic words and ISO dates. Adding a client to ``data/`` automatically extends the guard. Tests, docs,
evals and the data generator are allowed to name data; the runtime is not.
"""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.conftest import DATA, ROOT

SCANNED = [
    *(ROOT / "src").rglob("*.py"),
    *(ROOT / "config").rglob("*.yaml"),
    *(ROOT / "prompts").rglob("*.md"),
    *(ROOT / "agent").rglob("*.md"),
]
# Audit E.1: demo topic words and literal dates are never legitimate in the runtime.
PATTERNS = [
    re.compile(r"\b(wfh|work[- ]from[- ]home|breach|discount)\b", re.IGNORECASE),
    re.compile(r"\b20\d\d-\d\d-\d\d\b"),
]


def _frontmatter(p: Path) -> dict[str, object]:
    text = p.read_text()
    if not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except yaml.YAMLError:
        return {}


def _json_items(p: Path) -> Iterator[dict[str, object]]:
    if p.suffix == ".jsonl":
        for line in p.read_text().splitlines():
            if line.strip():
                yield json.loads(line)
        return
    data = json.loads(p.read_text())
    lists = [v for v in data.values() if isinstance(v, list)] if isinstance(data, dict) else [data]
    for items in lists or [[data]]:
        yield from (d for d in items if isinstance(d, dict))


def data_literals() -> set[str]:
    """Identifiers and names that only make sense for this one dataset."""
    out: set[str] = set()
    party_tokens: dict[str, set[str]] = {}
    for p in DATA.rglob("*.md"):
        fm = _frontmatter(p)
        for k in ("id", "doc_id"):
            if fm.get(k):
                out.add(str(fm[k]).split("@")[0])
        acct = fm.get("account")
        if acct:
            out.add(str(acct))
            for party in fm.get("parties") or []:
                for tok in re.findall(r"[A-Z][a-z]{3,}", str(party)):
                    party_tokens.setdefault(tok, set()).add(str(acct))
    # a party-name token that names exactly one client is that client's name (the company's own name spans all)
    out |= {t for t, accts in party_tokens.items() if len(accts) == 1}
    for p in DATA.rglob("*.json*"):
        for item in _json_items(p):
            rid = item.get("id")
            if isinstance(rid, str) and re.fullmatch(r"[A-Za-z]+[-_][\w-]+", rid):
                out.add(rid)
    with (DATA / "hris" / "workers.csv").open(newline="") as fh:
        for row in csv.DictReader(fh):
            out.add(row["worker_id"])
            out.add(row["location"])
    with (DATA / "policies" / "delegation_of_authority_matrix.csv").open(newline="") as fh:
        out |= {row["id"] for row in csv.DictReader(fh)}
    return {s for s in out if len(s) >= 3}


LITERALS = sorted(data_literals(), key=len, reverse=True)
_LIT = re.compile(r"(?<![\w-])(" + "|".join(re.escape(s) for s in LITERALS) + r")(?![\w-])")


def test_guard_has_teeth() -> None:
    assert len(LITERALS) > 100
    assert _LIT.search("talk to NW1101 about it") is not None


# Source-schema sentinels: values a source system uses as a wildcard, declared once in the source catalog.
# They describe the connector's schema, not a fact; anything else in config/sources.yaml is still checked.
# The directory role mapping (which IdP groups mean "unit leader" / "executive") is per-tenant connector config,
# like an SSO role mapping; it is the only place group ids may appear.
SCHEMA_SENTINELS = {"config/sources.yaml": {"ALL", "grp-unit-head", "grp-exec"}}


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_data_literals(path: Path) -> None:
    hits: list[str] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        for rx in (_LIT, *PATTERNS):
            for m in rx.finditer(line):
                if m.group(0) not in SCHEMA_SENTINELS.get(str(path.relative_to(ROOT)), set()):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {m.group(0)!r}")
    assert not hits, "data-specific literals in the runtime:\n" + "\n".join(hits[:40])
