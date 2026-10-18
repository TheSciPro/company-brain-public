-- 0005 Layer 2: facts with precedence/supersession, alerts, entity-resolution review queue, ingest log
CREATE TABLE facts (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  key TEXT NOT NULL, about TEXT NOT NULL, scope TEXT,             -- scope e.g. client ACC-BRM; NULL = general
  value TEXT NOT NULL, unit TEXT,
  valid_from DATE NOT NULL, valid_to DATE,
  authority INTEGER NOT NULL CHECK (authority BETWEEN 1 AND 5),
  status TEXT NOT NULL CHECK (status IN ('current','superseded','conflict','draft','corroborates')),
  src_type TEXT NOT NULL CHECK (src_type IN ('record','document')), src_id TEXT NOT NULL,
  quote TEXT NOT NULL,
  supersedes_id TEXT REFERENCES facts(id), overrides_id TEXT REFERENCES facts(id),
  related_id TEXT REFERENCES facts(id),                          -- conflict/draft/corroboration counterpart
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CHECK (valid_to IS NULL OR valid_to >= valid_from));
CREATE INDEX ix_facts_lookup ON facts(key, about, scope, valid_from);
CREATE INDEX ix_facts_src ON facts(src_id);
CREATE TABLE alerts (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  type TEXT NOT NULL CHECK (type IN ('conflict','stale','risky_action','leak','hr_discrepancy')),
  about TEXT, fact_id TEXT REFERENCES facts(id), other_fact_id TEXT REFERENCES facts(id),
  detail TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','dismissed')),
  evidence_hash TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE review_queue (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('same_as','fact','other')),
  subject TEXT NOT NULL, candidate_id TEXT, confidence REAL, context TEXT,
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','accepted','rejected')),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE ingest_log (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, item_id TEXT, status TEXT NOT NULL
    CHECK (status IN ('loaded','skipped','rejected')), reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX ix_grants_res_dates ON grants(resource_type, resource_id, valid_from);
