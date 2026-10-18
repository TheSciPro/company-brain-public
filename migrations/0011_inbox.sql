-- 0011 in-app inbox: heartbeat nudges and (R4) approval requests land here
CREATE TABLE IF NOT EXISTS inbox_messages (
  id          TEXT PRIMARY KEY,
  person_id   TEXT NOT NULL REFERENCES people(id),
  kind        TEXT NOT NULL CHECK (kind IN ('nudge', 'approval', 'system')),
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  ref         TEXT,
  dedupe_key  TEXT NOT NULL UNIQUE,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  read_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_inbox_person ON inbox_messages (person_id, created_at);
