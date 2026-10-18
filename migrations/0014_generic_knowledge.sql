-- 0014 generic knowledge: country from the directory, fact labels (keys are discovered from data), clients as
-- entities (from contract frontmatter), and the extraction ledger (what the LLM extracted from which doc hash).
ALTER TABLE people ADD COLUMN country TEXT;
ALTER TABLE facts ADD COLUMN label TEXT;
CREATE TABLE clients (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  name TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]', source_ids TEXT NOT NULL DEFAULT '[]');
CREATE TABLE extractions (
  doc_id TEXT NOT NULL, doc_hash TEXT NOT NULL, prompt_ref TEXT NOT NULL, model TEXT,
  facts INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (doc_id, doc_hash));
