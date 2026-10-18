"""Person-scoped reads use as_person. No function hands nw_app a company-wide read."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import psycopg
import pytest

from nwb.conf.models import ApprovalsConfig, AuthorityRule, DocumentSources, RecordSource, SourcesConfig
from nwb.db.app_role import enable_login
from nwb.db.database import PostgresDatabase
from nwb.knowledge.approvals import ApprovalMatrix
from nwb.knowledge.audit import DecisionAuditor
from tests.conftest import APP_ROLE_PASSWORD, PG_DSN, app_dsn, fresh_db

pytestmark = pytest.mark.postgres

_ASOF = date(2026, 9, 28)

_SEED = """
INSERT INTO companies (id, name) VALUES ('co-a', 'Company A'), ('co-b', 'Company B');
INSERT INTO org_units (id, company_id, kind, name, valid_from) VALUES
  ('unit-a', 'co-a', 'department', 'Delivery', DATE '2026-01-01');
INSERT INTO people (id, company_id, name) VALUES ('per-a', 'co-a', 'Person A');
INSERT INTO memberships (id, person_id, unit_id, valid_from) VALUES
  ('mem-a', 'per-a', 'unit-a', DATE '2026-01-01');
INSERT INTO documents (id, company_id, title, classification, blob_url, hash) VALUES
  ('doc-int', 'co-a', 'visible ledger', 'internal', 'blob://int', 'h1'),
  ('doc-sec', 'co-a', 'secret ledger', 'highly_confidential', 'blob://sec', 'h2'),
  ('doc-b', 'co-b', 'other ledger', 'internal', 'blob://b', 'h3');
INSERT INTO records (id, company_id, source, source_id, record_type, classification, payload, hash) VALUES
  ('rec-int', 'co-a', 't', 'int', 'matrix', 'internal', '{"item":"expense claim"}', 'r1'),
  ('rec-sec', 'co-a', 't', 'sec', 'matrix', 'highly_confidential', '{"item":"secret claim"}', 'r2');
"""


def _sources() -> SourcesConfig:
    return SourcesConfig(
        documents=DocumentSources(folders=(), authority=(AuthorityRule(authority="approved_policy"),)),
        records={
            "matrix": RecordSource(
                role="approval_matrix",
                fields={"action": "item", "approvers": ("approver_1",)},
            )
        },
    )


def test_person_reads_see_only_readable_rows(tmp_path: Path) -> None:
    owner = fresh_db("postgres", tmp_path)
    try:
        with owner.unit_of_work() as uow:
            uow.executescript(_SEED)
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            enable_login(conn, APP_ROLE_PASSWORD)
        app = PostgresDatabase(app_dsn(), max_size=1, require_app_role=True)
        try:
            with app.unit_of_work() as uow:
                anonymous = uow.fetchone("SELECT count(*) AS n FROM documents")
            assert anonymous is not None and anonymous["n"] == 0

            with app.unit_of_work(as_person="per-a", asof=_ASOF) as uow:
                docs = [str(r["id"]) for r in uow.fetchall("SELECT id FROM documents ORDER BY id")]
                recs = [str(r["id"]) for r in uow.fetchall("SELECT id FROM records ORDER BY id")]
            assert docs == ["doc-int"]
            assert recs == ["rec-int"]

            matrix = ApprovalMatrix(app, _sources(), ApprovalsConfig(), DecisionAuditor(app), frozenset())
            found = {row.record_id for row in matrix.rows(_ASOF, person_id="per-a")}
            assert found == {"rec-int"}
        finally:
            app.close()
    finally:
        owner.close()


def test_no_company_wide_catalog_function(tmp_path: Path) -> None:
    """The catalogs that returned unreadable titles, payloads and fact labels are gone after migrating."""
    owner = fresh_db("postgres", tmp_path)
    try:
        with owner.unit_of_work() as uow:
            gone = uow.fetchone(
                "SELECT to_regprocedure('nw_company_catalog(text, text, text)') AS company,"
                " to_regprocedure('nw_fact_key_catalog(text)') AS facts"
            )
        assert gone is not None and gone["company"] is None and gone["facts"] is None
    finally:
        owner.close()
