"""Not live-verified. Payments then Platform on local FalkorDB with a stub LLM, not a provider.

Graphiti is the only writer. ``group_id`` is ``{company}__{user}`` (its own Falkor graph, not the company
graph). The second episode contradicts the first, so the Payments edge gets ``invalid_at``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pytest.importorskip("graphiti_core", reason="graph extra is not installed; pip install -e '.[graph]'")

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, MemoryConfig
from nwb.identity import Actor
from nwb.kg.server import FalkorEndpoint, LocalFalkorServer
from nwb.memory.embeddings import HashingEmbedder
from nwb.memory.graphiti_memory import open_personal_memory
from nwb.memory.guards import LengthGuard, QualityGate, QualityGuard, QuoteGuard, SecretGuard

ROOT = Path(__file__).resolve().parents[2]
FIRST = datetime(2026, 10, 1, tzinfo=UTC)
SECOND = datetime(2026, 10, 11, tzinfo=UTC)


def test_team_move_sets_invalid_at_on_the_user_graph(tmp_path: Path) -> None:
    loader = ConfigLoader(ROOT / "config")
    graph_cfg = loader.load("graph", GraphConfig)
    memory_cfg = loader.load("memory", MemoryConfig)
    guards = [
        LengthGuard(memory_cfg.content_max_chars),
        SecretGuard(memory_cfg.secret_patterns),
        QualityGuard(QualityGate(memory_cfg.quality)),
        QuoteGuard(),
    ]
    actor = Actor("acme", "u1", date(2026, 10, 11))
    group_id = graph_cfg.user_group(actor.company_id, actor.person_id)
    assert group_id == "acme__u1"
    assert group_id != "nw"

    with LocalFalkorServer(tmp_path / "falkor") as server:
        memory = open_personal_memory(
            FalkorEndpoint(host=server.host, port=server.port),
            HashingEmbedder(memory_cfg.embedder.dim),
            _llm(),
            graph_cfg,
            memory_cfg,
            guards,
        )
        try:
            memory.remember_chat(
                actor,
                [("user", "I work on the Payments team.")],
                ["Payments", "Platform"],
                FIRST,
            )
            memory.remember_chat(
                actor,
                [("user", "I moved from the Payments team to the Platform team.")],
                ["Payments", "Platform", "User"],
                SECOND,
            )
            facts = memory.facts_as_of(actor, include_history=True)
        finally:
            memory.close()

    detail = [(f.text, f.valid_at, f.invalid_at, f.supersedes, f.superseded_by) for f in facts]
    payments = [f for f in facts if "Payments" in f.text and "Platform" not in f.text]
    platform = [f for f in facts if "Platform" in f.text]
    assert payments, detail
    assert platform, detail
    assert payments[0].invalid_at is not None, detail
    assert platform[0].invalid_at is None, detail
    assert payments[0].superseded_by == platform[0].id, detail
    current = memory_facts_current(facts, actor.asof)
    assert any("Platform" in f.text for f in current)
    assert all("Payments" not in f.text or "Platform" in f.text for f in current)


def memory_facts_current(facts: list, asof: date) -> list:
    return [f for f in facts if f.current_on(asof)]


def _llm():
    from tests.integration.team_move_llm import TeamMoveLLM

    return TeamMoveLLM()
