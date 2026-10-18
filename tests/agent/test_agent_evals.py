"""Agent-level evals (audit Part E.3): natural-language question -> the real agent graph -> assert on tool results.

Each case replays a cassette of recorded model responses (``tests/fixtures/cassettes/<id>.json``) so CI is
deterministic and offline; tools, skills, memory and the database run for real. Re-record with
``nwb eval --live`` (Groq). The assertion is on the *tool artifacts* and the citation in the answer, so a replayed
answer cannot pass if the tools no longer return the expected value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.agent.conftest import Harness
from tests.conftest import ROOT

CASES_FILE = ROOT / "tests" / "fixtures" / "evals" / "cases.yaml"
CASSETTES = ROOT / "tests" / "fixtures" / "cassettes"
CASES: list[dict[str, Any]] = yaml.safe_load(CASES_FILE.read_text())["cases"]

# Cases whose cassette is incomplete: the live recording ran out of the free-tier token budget before the model
# answered, so replay ends with "(script exhausted)". Re-record with ``scripts/record_evals.py <id>`` and delete the
# id here; strict=True makes a fixed case fail loudly until it is removed from this set.
INCOMPLETE_RECORDING = frozenset({"doa-travel-heldout", "memory-remember-preference"})


def _params() -> list[Any]:
    incomplete = pytest.mark.xfail(strict=True, reason="cassette incomplete (recording hit the token limit); re-record")
    return [pytest.param(c, id=c["id"], marks=[incomplete] if c["id"] in INCOMPLETE_RECORDING else []) for c in CASES]


@pytest.mark.parametrize("case", _params())
def test_agent_eval(harness: Harness, case: dict[str, Any]) -> None:
    from nwb.evals import EvalRunner

    cassette = CASSETTES / f"{case['id']}.json"
    assert cassette.exists(), f"record it: nwb eval --live --case {case['id']}"
    result = EvalRunner(harness.c).run_replay(case, cassette)
    assert result.passed, result.explain()


def test_every_case_has_a_cassette() -> None:
    missing = [c["id"] for c in CASES if not (CASSETTES / f"{c['id']}.json").exists()]
    assert not missing, missing


def test_incomplete_list_names_real_cases() -> None:
    assert {c["id"] for c in CASES} >= INCOMPLETE_RECORDING


def test_heldout_cases_exist() -> None:
    assert sum(1 for c in CASES if c.get("heldout")) >= 3
    assert Path(CASES_FILE).exists()
