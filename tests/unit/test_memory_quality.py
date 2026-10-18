"""Quality gate: venting, self-corrections, stack traces and error dumps are rejected before a write."""

from __future__ import annotations

import logging

import pytest

from nwb.conf import ConfigLoader, MemoryConfig
from nwb.memory.guards import MemoryWriteGate, QualityGate, SecretGuard
from tests.conftest import ROOT

VENTING = "ugh this is ridiculous, I hate this"
SELF_CORRECTION = "wait, no scratch that, I meant the other file"
STACK = 'Traceback (most recent call last):\n  File "worker.py", line 42, in run\nRuntimeError: boom'
DUMP = (
    "ERROR connection failed errno 111 while the worker was still starting up in the pool\n"
    "FATAL worker exception: panic in the pool after the socket dropped\n"
    "ERROR retry failed errno 111 and the upstream exception was not recovered\n"
    "the FATAL panic left the job failed and the exception was logged again\n"
)
DURABLE = "Prefers weekly status updates on Mondays."


def gate() -> MemoryWriteGate:
    cfg = ConfigLoader(ROOT / "config").load("memory", MemoryConfig)
    return MemoryWriteGate(SecretGuard(cfg.secret_patterns), QualityGate(cfg.quality))


@pytest.mark.parametrize(
    ("category", "text"),
    [("venting", VENTING), ("self_correction", SELF_CORRECTION), ("stack_trace", STACK), ("error_dump", DUMP)],
)
def test_each_noise_category_is_rejected(category: str, text: str) -> None:
    reason = gate().reject("/USER.md", text)
    assert reason is not None
    assert f"rejected ({category})" in reason
    assert "do not retry" in reason.lower()


def test_a_durable_line_and_other_files_are_kept(caplog: pytest.LogCaptureFixture) -> None:
    profile = gate()
    assert profile.reject("/USER.md", DURABLE) is None
    assert profile.reject("/notes.md", STACK) is None
    with caplog.at_level(logging.INFO, logger="nwb.memory.guards"):
        reason = profile.reject("/MEMORY.md", VENTING)
    assert reason and "venting" in reason
    assert caplog.records
    assert all(VENTING not in r.getMessage() for r in caplog.records)


def test_secret_guard_still_rejects_a_token_in_a_profile_file() -> None:
    reason = gate().reject("/USER.md", "the key is sk-abcdef1234567890abcdef")
    assert reason and "secret" in reason and "do not retry" in reason.lower()
