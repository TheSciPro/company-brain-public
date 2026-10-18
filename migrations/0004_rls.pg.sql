-- 0004 Postgres-only: RLS + nw_app role
-- PostgreSQL only. App connects as role nw_app (no BYPASSRLS) and sets per request:
--   SET LOCAL app.person_id = 'NW1103'; SET LOCAL app.asof = '2026-09-28';
CREATE OR REPLACE FUNCTION nw_me() RETURNS text LANGUAGE sql STABLE AS
  $$ SELECT current_setting('app.person_id', true) $$;
CREATE OR REPLACE FUNCTION nw_asof() RETURNS date LANGUAGE sql STABLE AS
  $$ SELECT COALESCE(NULLIF(current_setting('app.asof', true),'')::date, CURRENT_DATE) $$;

-- one function = the whole access rule; SECURITY DEFINER so it can read org tables the app role can't filter
CREATE OR REPLACE FUNCTION nw_can_read(p_company text, p_cls text, p_share text, p_owner_unit text,
                                       p_owner_person text, p_personal boolean, p_rtype text, p_rid text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  WITH me AS (SELECT p.id, p.company_id FROM people p WHERE p.id = nw_me() AND p.status = 'active'),
  mym AS (SELECT m.unit_id, m.is_leader FROM memberships m, me
          WHERE m.person_id = me.id AND m.valid_from <= nw_asof() AND (m.valid_to IS NULL OR m.valid_to >= nw_asof())),
  myg AS (SELECT g.group_id FROM group_members g, me
          WHERE g.person_id = me.id AND g.valid_from <= nw_asof() AND (g.valid_to IS NULL OR g.valid_to >= nw_asof()))
  SELECT EXISTS (SELECT 1 FROM me WHERE me.company_id = p_company) AND (
       (p_personal AND p_owner_person = nw_me())
    OR (NOT p_personal AND p_cls = 'public')
    OR (NOT p_personal AND p_cls = 'internal' AND EXISTS (SELECT 1 FROM mym))
    OR (NOT p_personal AND p_cls = 'confidential' AND p_share = 'team' AND EXISTS (
          SELECT 1 FROM mym JOIN org_unit_closure c ON c.descendant_id = mym.unit_id
          WHERE c.ancestor_id = p_owner_unit))                     -- owner is my unit or an ancestor of it
    OR (NOT p_personal AND p_cls = 'confidential' AND p_share = 'leadership' AND EXISTS (
          SELECT 1 FROM mym JOIN org_unit_closure c ON c.ancestor_id = mym.unit_id
          WHERE mym.is_leader AND c.descendant_id = p_owner_unit)) -- I lead the owner or a unit above it
    OR EXISTS (SELECT 1 FROM grants gr WHERE gr.resource_type = p_rtype AND gr.resource_id = p_rid
          AND gr.relation IN ('reader','editor') AND gr.valid_from <= nw_asof()
          AND (gr.valid_to IS NULL OR gr.valid_to >= nw_asof())
          AND ((gr.subject_type='person' AND gr.subject_id = nw_me())
            OR (gr.subject_type='group' AND gr.subject_id IN (SELECT group_id FROM myg))
            OR (gr.subject_type='unit' AND gr.subject_id IN (SELECT unit_id FROM mym)))));
$$;
-- The migrating owner must own this definer. That role also owns the tables, so the function
-- reads past RLS without BYPASSRLS. Do not FORCE ROW LEVEL SECURITY and do not reassign the
-- function to a superuser: a managed owner (Neon, Supabase, RDS) cannot do either.
ALTER FUNCTION nw_can_read(text, text, text, text, text, boolean, text, text) OWNER TO CURRENT_USER;

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE records   ENABLE ROW LEVEL SECURITY;
CREATE POLICY docs_read ON documents FOR SELECT
  USING (deleted_at IS NULL AND nw_can_read(company_id, classification, share_scope, owner_unit_id,
                                            owner_person_id, personal, 'document', id));
CREATE POLICY recs_read ON records FOR SELECT
  USING (nw_can_read(company_id, classification, share_scope, owner_unit_id,
                     owner_person_id, personal, 'record', id));

-- user-scoped tables: only your own rows
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['sessions','messages','agent_runs','memories','user_profiles',
                           'user_profile_versions','session_notes','connected_accounts','actions'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY own_rows ON %I USING (person_id = nw_me()) WITH CHECK (person_id = nw_me())', t);
  END LOOP; END $$;
-- approvers also see actions they must approve
CREATE POLICY approver_sees ON actions FOR SELECT
  USING (EXISTS (SELECT 1 FROM approvals a WHERE a.action_id = actions.id AND a.approver_id = nw_me()));

-- A new role is NOSUPERUSER, NOCREATEDB, NOCREATEROLE and NOBYPASSRLS by default.
-- Only a superuser may name the SUPERUSER attribute. NOBYPASSRLS is stated only when this
-- session may change BYPASSRLS (superuser, or a role that already has it). A managed owner
-- takes the plain NOLOGIN branch; the defaults still hold. LOGIN is enabled later, with a password.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nw_app') THEN
    RETURN;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = current_user AND (rolsuper OR rolbypassrls)
  ) THEN
    CREATE ROLE nw_app NOLOGIN NOBYPASSRLS;
  ELSE
    CREATE ROLE nw_app NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO nw_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nw_app;
GRANT INSERT, UPDATE ON sessions, messages, agent_runs, tool_calls, memories, memory_versions,
  user_profiles, user_profile_versions, session_notes, actions, approvals, graph_outbox, audit_log,
  onboarding_items, policy_acks, intents, nudges TO nw_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO nw_app;
