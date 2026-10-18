-- 0018: same drop as the SQLite file, after nwb migrate copied the rows into each owner's Graphiti group.
-- DROP TABLE removes the RLS policies and grants from 0004 and 0008. sessions.notes_status stays: post_session
-- still uses it.
-- Copy before drop: nwb migrate copies memories and session_notes first and records copied:<table>:<rows>
-- (src/nwb/db/copy_before_drop.py, src/nwb/memory/sql_copy.py). Without those exact markers, refuse.
-- memory_versions and memory_embeddings hang off memories and go with it.
DO $$
DECLARE
  t TEXT;
  n BIGINT;
BEGIN
  FOREACH t IN ARRAY ARRAY['memories', 'session_notes'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('SELECT count(*) FROM public.%I', t) INTO n;
      IF n > 0 AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE name = 'copied:' || t || ':' || n) THEN
        RAISE EXCEPTION '% was not copied to Graphiti; run nwb migrate, which copies it first', t;
      END IF;
    END IF;
  END LOOP;
END $$;
-- Children first so the foreign keys from memory_embeddings and memory_versions can go.
DROP TABLE IF EXISTS memory_embeddings;
DROP TABLE IF EXISTS memory_versions;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS session_notes;
