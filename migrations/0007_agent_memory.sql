-- 0007 R1 agent runtime: frozen per-session snapshot, turn counters, memory provenance/hash, embeddings
ALTER TABLE sessions ADD COLUMN turns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN last_memory_turn INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN profile_snapshot TEXT;
ALTER TABLE sessions ADD COLUMN bootstrap_shown BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE agent_runs ADD COLUMN thread_id TEXT;
ALTER TABLE agent_runs ADD COLUMN prompt_refs TEXT;
ALTER TABLE memories ADD COLUMN content_hash TEXT;
ALTER TABLE memories ADD COLUMN authority INTEGER NOT NULL DEFAULT 1 CHECK (authority = 1);
ALTER TABLE memories ADD COLUMN src_session_id TEXT REFERENCES sessions(id);
CREATE TABLE memory_embeddings (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id),
  person_id TEXT NOT NULL REFERENCES people(id),
  model TEXT NOT NULL, dim INTEGER NOT NULL, vector TEXT NOT NULL,      -- JSON array of floats (portable)
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX ix_mememb_person ON memory_embeddings(person_id);
CREATE INDEX ix_mems_hash ON memories(person_id, content_hash);
CREATE INDEX ix_notes_person ON session_notes(person_id, created_at);
CREATE INDEX ix_profile_versions ON user_profile_versions(person_id, version);
