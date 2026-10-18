"""Catalog split and graph-name formatting. No FalkorDB."""

from __future__ import annotations

import json
from pathlib import Path

from nwb.conf.loader import ConfigLoader
from nwb.conf.models import GraphConfig, GraphLoadConfig, LinkDecl, ProseDecl, TableDecl
from nwb.ingest.acl import classify_tag, is_personal, needs_grants
from nwb.kg.catalog import materialise

ROOT = Path(__file__).resolve().parents[2]


def test_company_graph_name_is_formatted() -> None:
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    assert cfg.company_graph("acme", "internal") == "acme_kb_internal"
    assert cfg.company_graph("acme", "confidential", prefix="pre_") == "pre_acme_kb_confidential"
    assert "partition_suffix" not in cfg.company.model_dump()


def test_tag_map_personal_and_fail_closed() -> None:
    assert is_personal("PERSONAL")
    assert not is_personal("PUBLIC-INT")
    assert not needs_grants("PUBLIC")
    assert classify_tag("NOT-A-REAL-TAG") == ("highly_confidential", None)


def test_materialise_splits_rows_by_level(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "people.csv").write_text(
        "worker_id,name,title,grade,unit,manager_id,classification\n"
        "a1,Ada Roe,Engineer,G1,Core,,PUBLIC-INT\n"
        "a2,Bea Poe,Lead,G2,Core,a1,MGR\n",
        encoding="utf-8",
    )
    notes = data / "notes"
    notes.mkdir()
    (notes / "one.md").write_text("---\nclassification: MGR\n---\n\nAda Roe leads Core Desk.\n", encoding="utf-8")
    load = GraphLoadConfig(
        boundaries="notes",
        tables=(
            TableDecl(
                path="people.csv",
                source_name="people.csv",
                label="Person",
                key="worker_id",
                name="name",
                properties=("title",),
                links=(LinkDecl(type="REPORTS_TO", to="Person", by="manager_id"),),
                standalone=False,
            ),
        ),
        prose=(ProseDecl(root="notes"),),
    )
    bundles = materialise(data, load, tmp_path / "work", ("public", "internal", "confidential", "highly_confidential"))
    by_level = {b.level: b for b in bundles}
    assert by_level["internal"].table_rows == 1
    assert by_level["confidential"].table_rows == 1
    assert by_level["confidential"].prose[0].document_id == "notes/one.md"
    internal_csv = (by_level["internal"].directory / "people.csv").read_text(encoding="utf-8")
    assert "Ada Roe" in internal_csv
    assert "Bea Poe" not in internal_csv


def test_configured_sources_exist() -> None:
    cfg = ConfigLoader(ROOT / "config").load("graph", GraphConfig)
    assert cfg.company.load is not None
    for table in cfg.company.load.tables:
        assert (ROOT / "data" / table.path).is_file(), table.path
    payload = json.loads((ROOT / "data" / "approvals" / "pricing_exceptions.json").read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload
