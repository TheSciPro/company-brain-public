"""finalize() on a local FalkorDB: same-name people stay apart; a near-duplicate org is inbox-only."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("graphrag_sdk", reason="graph extra is not installed; pip install -e '.[graph]'")

from nwb.actions.service import ActionService
from nwb.conf.loader import ConfigLoader
from nwb.conf.models import ActionsConfig, GraphConfig, ResolutionConfig, TableDecl
from nwb.graph.client import connect_server
from nwb.kg.load import load_company
from nwb.kg.offline_llm import OfflineSdkLLM
from nwb.kg.ontology import declared_ontology
from nwb.kg.resolution import ApprovalInbox
from nwb.kg.server import falkor_endpoint
from nwb.memory.embeddings import HashingEmbedder
from tests.conftest import fresh_db

ROOT = Path(__file__).resolve().parents[2]
_LOCAL = {"127.0.0.1", "localhost", "::1"}


def _local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    original = socket.socket.connect

    def _guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in _LOCAL:
            raise AssertionError(f"load opened a network connection to {address!r}")
        original(self, address)

    monkeypatch.setattr(socket.socket, "connect", _guard)


def _cfg() -> GraphConfig:
    base = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    assert base.company.load is not None
    people = TableDecl(
        path="people.csv",
        format="csv",
        source_name="people.csv",
        label="Person",
        key="id",
        name="name",
        properties=("email",),
    )
    orgs = TableDecl(
        path="orgs.csv",
        format="csv",
        source_name="orgs.csv",
        label="Organization",
        key="id",
        name="name",
    )
    load = base.company.load.model_copy(update={"tables": (people, orgs), "prose": ()})
    return base.model_copy(update={"company": base.company.model_copy(update={"load": load})})


def _write_sources(directory: Path) -> None:
    directory.mkdir()
    (directory / "people.csv").write_text(
        "id,name,email\n"
        "p-1,Ada Lovelace,ada@example.com\n"
        "p-2,Ada Lovelace,ada.other@example.com\n"
        "p-3,Augusta Ada,ada@example.com\n",
        encoding="utf-8",
    )
    (directory / "orgs.csv").write_text(
        "id,name\no-1,Acme Company\no-2,Acme\n",
        encoding="utf-8",
    )


def _props(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.startswith("{"):
        loaded = json.loads(raw)
        return dict(loaded) if isinstance(loaded, dict) else {}
    if isinstance(raw, list):
        return {str(item[0]): item[1] for item in raw if isinstance(item, (list, tuple)) and len(item) == 2}
    return {}


def _field(props: dict[str, Any], logical: str) -> str:
    direct = props.get(logical)
    if direct not in (None, ""):
        return str(direct)
    suffix = f"__{logical}"
    found = [value for key, value in props.items() if str(key).endswith(suffix) and value not in (None, "")]
    return "" if len(found) != 1 else str(found[0])


def test_finalize_keeps_people_apart_and_suggests_the_org(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _local_only(monkeypatch)
    _write_sources(tmp_path / "src")
    db = fresh_db("sqlite", tmp_path)
    with db.unit_of_work() as uow:
        uow.execute("INSERT INTO companies (id, name) VALUES ('fixture', 'Fixture')")
        uow.execute(
            "INSERT INTO people (id, company_id, name, email) VALUES "
            "('rev-1', 'fixture', 'Reviewer', 'reviewer@example.com')"
        )
    resolution = ConfigLoader(ROOT / "config").load("resolution", ResolutionConfig)
    actions = ActionService(db, ConfigLoader(ROOT / "config").load("actions", ActionsConfig))
    sink = ApprovalInbox(actions, resolution.inbox.kind, resolution.inbox.proposed_by)
    prefix = "b4res_"
    company = "fixture"
    with falkor_endpoint(None, tmp_path / "falkor") as endpoint:
        cfg = _cfg()
        assert cfg.company.load is not None
        report = asyncio.run(
            load_company(
                cfg,
                data_dir=tmp_path / "src",
                company=company,
                endpoint=endpoint,
                llm=OfflineSdkLLM("offline-kg"),
                embedder=HashingEmbedder(),
                work_dir=tmp_path / "work",
                prefix=prefix,
                tables_only=True,
                resolution=resolution,
                suggestions=sink,
                reviewer="rev-1",
                asof=date(2026, 1, 15),
                llm_label="offline",
                ontology=declared_ontology(cfg.company.load),
            )
        )
        assert len(report.levels) == 1
        level = report.levels[0]
        assert level.links == 1
        assert level.suggestions == 1
        assert level.filed == 1
        store = connect_server(endpoint.host, endpoint.port, level.graph, timeout_ms=30_000)
        people = store.ro_query("MATCH (p:`Person`) RETURN p.entity_key, p.name, properties(p)")
        orgs = store.ro_query("MATCH (o:`Organization`) RETURN o.entity_key, o.name")
        links = store.ro_query("MATCH (a)-[r:IDENTIFIES]->(b) RETURN a.entity_key, b.entity_key, r.field")
    adas = [row for row in people if row[1] == "Ada Lovelace"]
    assert len(adas) == 2
    assert {row[0] for row in people} == {"p-1", "p-2", "p-3"}
    emails = {_field(_props(row[2]), "email") for row in adas}
    assert emails == {"ada@example.com", "ada.other@example.com"}
    assert {row[1] for row in orgs} == {"Acme Company", "Acme"}
    assert {row[0] for row in orgs} == {"o-1", "o-2"}
    assert len(links) == 1
    pair = tuple(sorted((links[0][0], links[0][1])))
    assert pair == ("p-1", "p-3")
    assert links[0][2] == "email"
    with db.unit_of_work() as uow:
        inbox = uow.fetchall("SELECT kind, title, body FROM inbox_messages")
        actions_rows = uow.fetchall("SELECT title FROM actions")
    assert len(inbox) == 1
    assert inbox[0]["kind"] == "approval"
    assert "Nothing was merged" in str(inbox[0]["body"])
    title = str(inbox[0]["title"])
    assert "Acme Company" in title
    assert "Acme" in title.replace("Acme Company", "")
    assert "Ada" not in title
    assert "Ada" not in str(inbox[0]["body"])
    assert len(actions_rows) == 1
    db.close()
