-- 0021 Session checks. 0018 and 0019 belong to other branches; 0020 is unused.
--
-- actions UPDATE: WITH CHECK must be at least as strict as USING, and person_id must stay the
-- requester. A subquery in WITH CHECK would see the new row, so nw_action_requester reads the
-- stored requester from the pre-update snapshot (SECURITY DEFINER, STABLE). It answers only for an
-- action the session person may already update (own row or assigned approver); any other id is NULL.
--
-- No company-wide SECURITY DEFINER reads. nw_company_catalog and nw_fact_key_catalog returned rows the
-- session person may not read (titles, blob URLs, record payloads, fact labels). Company search reads the
-- per-level graphs and the fact resolver reads facts as the person, so both are dropped where an earlier
-- draft of this branch created them.
DROP FUNCTION IF EXISTS nw_company_catalog(text, text, text);
DROP FUNCTION IF EXISTS nw_fact_key_catalog(text);

CREATE OR REPLACE FUNCTION nw_action_requester(p_action text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT a.person_id FROM actions a
  WHERE a.id = p_action AND (nw_user_visible(a.person_id) OR nw_is_approver(a.id));
$$;
ALTER FUNCTION nw_action_requester(text) OWNER TO CURRENT_USER;
REVOKE ALL ON FUNCTION nw_action_requester(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION nw_action_requester(text) TO nw_app;

DROP POLICY IF EXISTS actions_update ON actions;
CREATE POLICY actions_update ON actions FOR UPDATE
  USING (nw_user_visible(person_id) OR nw_is_approver(id))
  WITH CHECK (
    (nw_user_visible(person_id) OR nw_is_approver(id))
    AND person_id = nw_action_requester(id)
  );
