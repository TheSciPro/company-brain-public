-- 0019 Drop the SQL facts table, after nwb migrate copied the rows into the per-level graphs.
-- Copy before drop: nwb migrate copies every fact first and records copied:facts:<rows>
-- (src/nwb/db/copy_before_drop.py). Without that exact marker, the CHECK below fails and nothing is dropped.
CREATE TEMP TABLE facts_copy_guard (
  ok INTEGER CONSTRAINT facts_not_copied_to_graphs_run_nwb_migrate CHECK (ok = 1)
);
INSERT INTO facts_copy_guard (ok)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM facts) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM schema_migrations WHERE name = 'copied:facts:' || (SELECT count(*) FROM facts)
  ) THEN 1
  ELSE 0
END;
DROP TABLE facts_copy_guard;
-- alerts.fact_id referenced facts. SQLite will not drop a referenced table while foreign_keys
-- is on, and that pragma cannot change inside this transaction. Rebuild alerts without the
-- reference, then drop facts. IF EXISTS so a database that never had the table still migrates.
CREATE TABLE alerts_kept (
  id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id),
  type TEXT NOT NULL CHECK (type IN ('conflict', 'stale', 'risky_action', 'leak', 'hr_discrepancy')),
  about TEXT,
  fact_id TEXT,
  other_fact_id TEXT,
  detail TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
  evidence_hash TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO alerts_kept (
  id, company_id, type, about, fact_id, other_fact_id, detail, status, evidence_hash, created_at
)
SELECT id, company_id, type, about, fact_id, other_fact_id, detail, status, evidence_hash, created_at
FROM alerts;
DROP TABLE alerts;
ALTER TABLE alerts_kept RENAME TO alerts;
DROP TABLE IF EXISTS facts;
