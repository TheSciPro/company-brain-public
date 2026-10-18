"""One live consolidation call when NVIDIA_API_KEY is set. Scripted memory, no FalkorDB.

The key is read only to build the client. It is never printed.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_openai import ChatOpenAI

from nwb.container import Container
from nwb.db.repo_sessions import SessionRepository
from nwb.identity import Actor
from tests.agent.conftest import ASOF, hashing_memory_config
from tests.agent.conftest import test_settings as build_settings
from tests.agent.scripted_memory import ScriptedPersonalMemory

pytestmark = pytest.mark.live

_MODEL = "openai/gpt-oss-20b"


@pytest.fixture
def live_company(company_template: Path, tmp_path: Path) -> Iterator[Container]:
    db_path = tmp_path / "nwb.sqlite"
    shutil.copy(company_template, db_path)
    container = Container.from_settings(build_settings(db_path, company_template.parent / "blobs"))
    container.memory_config = hashing_memory_config(container)
    container.personal_memory = ScriptedPersonalMemory(
        container.graph_config, container.memory_config, container.write_guards()
    )
    container._sync_graph = lambda: None  # type: ignore[method-assign]
    try:
        yield container
    finally:
        container.close()


def test_live_nvidia_consolidation_does_not_delete(live_company: Container) -> None:
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        pytest.skip("NVIDIA_API_KEY is not set")
    live = ChatOpenAI(
        model=_MODEL,
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=key,
        temperature=0,
        max_tokens=512,
        timeout=60,
        max_retries=0,
        reasoning_effort="low",
    )
    container = live_company
    container.llm.chat = lambda _ref, _role=None: live  # type: ignore[method-assign]
    container.__dict__.pop("models", None)
    runner = container.runner
    container.consolidation._spec = container.consolidation._spec.model_copy(update={"model_call_limit": 2})
    sid = runner.start_session("NW1103", ASOF)
    company = container.settings.company_id
    before = container.memory_files.read(company, "NW1103", "USER.md") or ""
    assert "NW1103" in before
    actor = Actor(company, "NW1103", ASOF)
    facts_before = len(container.personal_memory.facts_as_of(actor, include_history=True))
    with container.db.unit_of_work(as_person="NW1103") as uow:
        repo = SessionRepository(uow)
        for text in ("I like short answers.", "I work on the Payments team.", "Amounts in INR please."):
            repo.add_message(sid, "NW1103", "user", text)
    report = container.consolidation.on_session_end("NW1103", sid)
    assert report.status == "done", report.status
    after = container.memory_files.read(company, "NW1103", "USER.md") or ""
    assert "NW1103" in after
    facts_after = len(container.personal_memory.facts_as_of(actor, include_history=True))
    assert facts_after >= facts_before
