-- 0009 decision audit: what the brain asserted, to whom, as of when, citing which sources (R2)
CREATE TABLE IF NOT EXISTS decision_audits (
  id          TEXT PRIMARY KEY,
  company_id  TEXT NOT NULL REFERENCES companies(id),
  person_id   TEXT NOT NULL REFERENCES people(id),
  session_id  TEXT,
  run_id      TEXT,
  asof        DATE NOT NULL,
  on_date     DATE NOT NULL,
  tool        TEXT NOT NULL,
  question    TEXT NOT NULL,
  answer      TEXT NOT NULL,
  citations   TEXT NOT NULL DEFAULT '[]',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_decision_audits_person ON decision_audits (person_id, created_at);
