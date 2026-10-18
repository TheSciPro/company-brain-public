-- 0010 Postgres: a person reads their own decision audits; auditors read via the audit role (not the app role)
ALTER TABLE decision_audits ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON decision_audits USING (person_id = nw_me()) WITH CHECK (person_id = nw_me());
GRANT SELECT, INSERT ON decision_audits TO nw_app;
