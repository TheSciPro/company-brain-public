-- 0012 Postgres: inbox is owner-only
ALTER TABLE inbox_messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON inbox_messages USING (person_id = nw_me()) WITH CHECK (person_id = nw_me());
GRANT SELECT, INSERT, UPDATE ON inbox_messages TO nw_app;
