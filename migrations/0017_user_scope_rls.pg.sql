-- 0017 User-scoped RLS is per person and per company, and nw_app gets least privilege.
-- Numbered 0017 because 0016_onboarding_beats.sql lands first.
-- The runtime connects *as* nw_app. This file does not set a password and does not force NOLOGIN,
-- so a later ALTER ROLE ... LOGIN survives. Cross-user jobs stay on the owner URL (NWB_MIGRATE_URL).
-- Company checks use SECURITY DEFINER helpers so policies do not recurse through RLS.
-- Those helpers stay owned by the migrating owner. A managed owner is not a superuser, so this
-- file never ALTERs the SUPERUSER, CREATEDB, or BYPASSRLS attributes and never FORCE ROW LEVEL SECURITY.

CREATE OR REPLACE FUNCTION nw_company() RETURNS text
LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('app.company_id', true), '') $$;

CREATE OR REPLACE FUNCTION nw_same_company(p_person text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT nw_company() IS NOT NULL
     AND EXISTS (SELECT 1 FROM people p WHERE p.id = p_person AND p.company_id = nw_company());
$$;
ALTER FUNCTION nw_same_company(text) OWNER TO CURRENT_USER;

-- Own row, and the person belongs to the company pinned on this transaction. Unset company => no rows.
CREATE OR REPLACE FUNCTION nw_user_visible(p_person text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT p_person IS NOT NULL AND p_person = nw_me() AND nw_same_company(p_person);
$$;

CREATE OR REPLACE FUNCTION nw_is_approver(p_action text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM approvals a
    JOIN people p ON p.id = a.approver_id
    WHERE a.action_id = p_action AND a.approver_id = nw_me() AND p.company_id = nw_company()
  );
$$;
ALTER FUNCTION nw_is_approver(text) OWNER TO CURRENT_USER;

CREATE OR REPLACE FUNCTION nw_owns_action(p_action text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM actions a
    JOIN people p ON p.id = a.person_id
    WHERE a.id = p_action AND a.person_id = nw_me() AND p.company_id = nw_company()
  );
$$;
ALTER FUNCTION nw_owns_action(text) OWNER TO CURRENT_USER;

-- Uniform owner-only tables (person_id column).
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY[
    'sessions','messages','agent_runs','memories','user_profiles','user_profile_versions',
    'session_notes','connected_accounts','memory_embeddings',
    'user_identities','onboarding_plans','onboarding_items','policy_acks','intents','nudges'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS own_rows ON %I', t);
    EXECUTE format(
      'CREATE POLICY own_rows ON %I USING (nw_user_visible(person_id)) WITH CHECK (nw_user_visible(person_id))',
      t);
  END LOOP;
END $$;

-- actions: the requester owns the row; the assigned approver can read and update status.
ALTER TABLE actions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON actions;
DROP POLICY IF EXISTS approver_sees ON actions;
CREATE POLICY actions_select ON actions FOR SELECT
  USING (nw_user_visible(person_id) OR nw_is_approver(id));
CREATE POLICY actions_insert ON actions FOR INSERT
  WITH CHECK (nw_user_visible(person_id));
CREATE POLICY actions_update ON actions FOR UPDATE
  USING (nw_user_visible(person_id) OR nw_is_approver(id))
  WITH CHECK (nw_same_company(person_id));

-- approvals: requester inserts a row for an approver in the same company; only that approver updates it.
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON approvals;
CREATE POLICY approvals_select ON approvals FOR SELECT
  USING (nw_user_visible(approver_id) OR nw_owns_action(action_id));
CREATE POLICY approvals_insert ON approvals FOR INSERT
  WITH CHECK (nw_same_company(approver_id) AND nw_owns_action(action_id));
CREATE POLICY approvals_update ON approvals FOR UPDATE
  USING (nw_user_visible(approver_id))
  WITH CHECK (nw_user_visible(approver_id));

-- inbox: a person reads and updates their own messages. Insert is company-scoped so the app can
-- deliver an approval card to the approver. It cannot insert a row for another company.
ALTER TABLE inbox_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON inbox_messages;
CREATE POLICY inbox_select ON inbox_messages FOR SELECT USING (nw_user_visible(person_id));
CREATE POLICY inbox_update ON inbox_messages FOR UPDATE
  USING (nw_user_visible(person_id)) WITH CHECK (nw_user_visible(person_id));
CREATE POLICY inbox_insert ON inbox_messages FOR INSERT WITH CHECK (nw_same_company(person_id));

ALTER TABLE decision_audits ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON decision_audits;
CREATE POLICY own_rows ON decision_audits
  USING (nw_user_visible(person_id) AND company_id = nw_company())
  WITH CHECK (nw_user_visible(person_id) AND company_id = nw_company());

ALTER TABLE memory_versions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON memory_versions;
CREATE POLICY own_rows ON memory_versions
  USING (EXISTS (SELECT 1 FROM memories m WHERE m.id = memory_versions.memory_id AND nw_user_visible(m.person_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM memories m WHERE m.id = memory_versions.memory_id AND nw_user_visible(m.person_id)));

ALTER TABLE tool_calls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON tool_calls;
CREATE POLICY own_rows ON tool_calls
  USING (EXISTS (SELECT 1 FROM agent_runs r WHERE r.id = tool_calls.agent_run_id AND nw_user_visible(r.person_id)))
  WITH CHECK (EXISTS (
    SELECT 1 FROM agent_runs r WHERE r.id = tool_calls.agent_run_id AND nw_user_visible(r.person_id)));

ALTER TABLE connector_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON connector_events;
CREATE POLICY own_rows ON connector_events
  USING (EXISTS (
    SELECT 1 FROM connected_accounts a
    WHERE a.id = connector_events.connected_account_id AND nw_user_visible(a.person_id)))
  WITH CHECK (EXISTS (
    SELECT 1 FROM connected_accounts a
    WHERE a.id = connector_events.connected_account_id AND nw_user_visible(a.person_id)));

-- post_session finishes a run it claimed on the owner connection, after SET ROLE nw_app.
ALTER TABLE job_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_rows ON job_runs;
CREATE POLICY job_runs_session_owner ON job_runs
  USING (scope_type = 'session' AND EXISTS (
    SELECT 1 FROM sessions s WHERE s.id = job_runs.scope_id AND nw_user_visible(s.person_id)))
  WITH CHECK (scope_type = 'session' AND EXISTS (
    SELECT 1 FROM sessions s WHERE s.id = job_runs.scope_id AND nw_user_visible(s.person_id)));

-- Least privilege. Directory, classification and company config stay readable (they are not user-scoped).
-- onboarding_templates is company configuration, not a per-user table, so it has no RLS.
-- jobs, graph_outbox reads, audit_log, review_queue, ingest_log and alerts stay with the owner.
-- A superuser may restate the safe role attributes. A managed owner must not: ALTER ROLE
-- NOSUPERUSER / NOCREATEDB / NOBYPASSRLS is denied unless current_user already has that attribute.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper) THEN
    ALTER ROLE nw_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
  END IF;
END $$;
-- Revoke and re-grant only objects this owner created. ALL TABLES / ALL FUNCTIONS fails on Neon
-- when public holds an object owned by another role.
DO $$
DECLARE rel record;
BEGIN
  FOR rel IN
    SELECT c.relname, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relowner = CURRENT_USER::regrole
      AND c.relkind IN ('r', 'S')
  LOOP
    IF rel.relkind = 'S' THEN
      EXECUTE format('REVOKE ALL ON SEQUENCE %I FROM nw_app', rel.relname);
    ELSE
      EXECUTE format('REVOKE ALL ON TABLE %I FROM nw_app', rel.relname);
    END IF;
  END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO nw_app;
GRANT SELECT ON
  companies, org_units, org_unit_closure, people, memberships, reporting_lines, groups, group_members,
  classification_levels, access_rules, access_rule_subjects, grants, documents, records, facts,
  clients, extractions,
  user_identities, connected_accounts, sessions, messages, agent_runs, tool_calls,
  actions, approvals, memories, memory_versions, memory_embeddings,
  user_profiles, user_profile_versions, session_notes,
  onboarding_templates, onboarding_plans, onboarding_items, policy_acks, intents, nudges,
  inbox_messages, decision_audits
TO nw_app;
GRANT INSERT, UPDATE ON
  sessions, messages, agent_runs, tool_calls,
  memories, memory_versions, memory_embeddings,
  user_profiles, user_profile_versions, session_notes,
  actions, approvals, connected_accounts,
  onboarding_items, policy_acks, intents, nudges, inbox_messages
TO nw_app;
GRANT UPDATE ON onboarding_plans, job_runs TO nw_app;
GRANT SELECT ON job_runs TO nw_app;
GRANT INSERT ON decision_audits, graph_outbox TO nw_app;
DO $$
DECLARE rel record; fn regprocedure;
BEGIN
  FOR rel IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'S' AND c.relowner = CURRENT_USER::regrole
  LOOP
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO nw_app', rel.relname);
  END LOOP;
  FOR fn IN
    SELECT p.oid::regprocedure
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proowner = CURRENT_USER::regrole
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO CURRENT_USER', fn);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO nw_app', fn);
  END LOOP;
END $$;
