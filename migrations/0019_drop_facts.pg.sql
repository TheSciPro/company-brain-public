-- 0019 Drop the SQL facts table and its facts_read policy, after nwb migrate copied the rows.
-- Company facts live in the per-level FalkorDB graphs. This runs as the table owner
-- (Neon neondb_owner): no ALTER ROLE. Dependent foreign keys on alerts are dropped with the table.
DO $$
BEGIN
  IF to_regclass('public.facts') IS NULL THEN
    RETURN;
  END IF;
  -- Copy before drop: nwb migrate copies every fact into the per-level graphs first and records
  -- copied:facts:<rows> (src/nwb/db/copy_before_drop.py). Without that exact marker, refuse.
  IF EXISTS (SELECT 1 FROM public.facts) AND NOT EXISTS (
    SELECT 1 FROM schema_migrations WHERE name = 'copied:facts:' || (SELECT count(*) FROM public.facts)
  ) THEN
    RAISE EXCEPTION 'facts were not copied to the fact graphs; run nwb migrate, which copies them first';
  END IF;
  EXECUTE 'DROP POLICY IF EXISTS facts_read ON public.facts';
  EXECUTE 'DROP TABLE public.facts CASCADE';
END $$;
