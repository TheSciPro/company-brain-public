-- 0008 Postgres: owner-only RLS for the new memory tables; app role may write them
ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON memory_embeddings USING (person_id = nw_me()) WITH CHECK (person_id = nw_me());
GRANT SELECT, INSERT, UPDATE ON memory_embeddings TO nw_app;
