-- 0018: drop the SQL memory tables, after nwb migrate copied the rows into each owner's Graphiti group.
-- Dated facts live in Graphiti; preferences live in /memories/ files. sessions.notes_status stays: post_session
-- still uses it.
-- Copy before drop: nwb migrate copies memories and session_notes first and records copied:<table>:<rows>
-- (src/nwb/db/copy_before_drop.py, src/nwb/memory/sql_copy.py). Without those exact markers, the CHECK below
-- fails and nothing is dropped. memory_versions and memory_embeddings hang off memories and go with it.
CREATE TEMP TABLE memory_copy_guard (
  ok INTEGER CONSTRAINT memory_not_copied_to_graphiti_run_nwb_migrate CHECK (ok = 1)
);
INSERT INTO memory_copy_guard (ok)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM memories) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM schema_migrations WHERE name = 'copied:memories:' || (SELECT count(*) FROM memories)
  ) THEN 1
  ELSE 0
END;
INSERT INTO memory_copy_guard (ok)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM session_notes) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM schema_migrations WHERE name = 'copied:session_notes:' || (SELECT count(*) FROM session_notes)
  ) THEN 1
  ELSE 0
END;
DROP TABLE memory_copy_guard;
-- Children first so foreign_keys = ON can drop the parents.
DROP TABLE IF EXISTS memory_embeddings;
DROP TABLE IF EXISTS memory_versions;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS session_notes;
