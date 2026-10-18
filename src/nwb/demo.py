"""`nwb demo`: run the held-out eval cases end to end through the agent (live LLM) and print each transcript."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from nwb.container import Container
from nwb.evals import EvalRunner, load_cases


def run_demo(c: Container, cases_file: Path, *, only: str | None = None, out: TextIO = sys.stdout) -> int:
    runner = EvalRunner(c)
    failed = 0
    cases = [k for k in load_cases(cases_file) if only in (None, k.id)]
    for case in cases:
        res = runner.run_live(case)
        failed += not res.passed
        out.write(f"[{'PASS' if res.passed else 'FAIL'}] {case.id}\n{res.explain()}\n\n")
    out.write(f"{len(cases) - failed}/{len(cases)} cases pass\n")
    return 1 if failed else 0
