-- 0006 Postgres: a fact is visible iff its source record/document is visible (RLS applies inside the subquery)
ALTER TABLE facts ENABLE ROW LEVEL SECURITY;
CREATE POLICY facts_read ON facts FOR SELECT USING (
  (src_type = 'record' AND EXISTS (SELECT 1 FROM records r WHERE r.id = facts.src_id))
  OR (src_type = 'document' AND EXISTS (SELECT 1 FROM documents d WHERE d.id = facts.src_id)));
GRANT SELECT ON facts, alerts, review_queue, ingest_log TO nw_app;
