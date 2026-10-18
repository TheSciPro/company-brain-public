"""Record agent evals live against the configured LLM chain on a throwaway, hermetic company database.

Usage:  PYTHONPATH=src:. .venv/bin/python scripts/record_evals.py [case-id ...]
        EVAL_LLM_MAIN / EVAL_LLM_BACKGROUND / EVAL_LLM_GRAPH override a role's provider chain
        (comma-separated ``provider:model``).

Loads data/ into a temporary SQLite database with the recorded fact extractions (no LLM at ingest), then asks each
case's question through the whole agent with the real models. Cassettes go to tests/fixtures/cassettes/ (replayed
by tests/agent/test_agent_evals.py) and readable transcripts to docs/transcripts/evals/.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.agent.conftest import hashing_memory_config, test_settings
from tests.conftest import MIGRATIONS, ROOT

from nwb.agent.clock import FixedClock
from nwb.container import Container
from nwb.db.migrate import Migrator
from nwb.evals import EvalRunner, load_cases
from nwb.obs.log import get_logger, log_step, setup_logging

CASES = ROOT / "tests" / "fixtures" / "evals" / "cases.yaml"
CASSETTES = ROOT / "tests" / "fixtures" / "cassettes"
TRANSCRIPTS = ROOT / "docs" / "transcripts" / "evals"
log = get_logger("nwb.scripts.record_evals")


def main(only: list[str]) -> int:
    # tests.conftest drops LLM_* for hermetic tests; a recording run opts back in explicitly.
    for var in ("LLM_MAIN", "LLM_BACKGROUND", "LLM_GRAPH"):
        if os.environ.get(f"EVAL_{var}"):
            os.environ[var] = os.environ[f"EVAL_{var}"]
    tmp = Path(tempfile.mkdtemp(prefix="nwb-evals-"))
    settings = test_settings(tmp / "company.sqlite", tmp / "blobs")
    setup_logging(settings)
    log.info("recording evals", workdir=str(tmp), cases=only or "all")
    c = Container.from_settings(settings)
    Migrator(c.db, MIGRATIONS).migrate()
    c.layer1_loader().load()
    c.layer2_loader().load()
    c.memory_config = hashing_memory_config(c)
    c._sync_graph = lambda: None  # type: ignore[method-assign]
    cases = [k for k in load_cases(CASES) if not only or k.id in only]
    runner, failed = EvalRunner(c), 0
    for case in cases:
        asof = runner._asof(case)
        c.clock = FixedClock(datetime(asof.year, asof.month, asof.day, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
        with log_step("eval.case", logger=log, level=logging.INFO, case=case.id, asof=asof.isoformat()):
            res = runner.run_live(case, CASSETTES, TRANSCRIPTS)
        failed += not res.passed
        print(res.explain(), end="\n\n", flush=True)  # the result itself is program output, not a log line
        log.info("waiting 20s for the free-tier token budget")
        time.sleep(20)  # space free-tier requests (NVIDIA ~40 RPM, OpenRouter 50/day)
    c.close()
    print(f"{len(cases) - failed}/{len(cases)} eval cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
