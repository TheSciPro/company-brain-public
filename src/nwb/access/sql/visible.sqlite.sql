-- SQLite / app-level equivalent of nw_can_read for table {table} (resource type '{rtype}'). Params: :me, :asof.
WITH me AS (SELECT id, company_id FROM people WHERE id = :me AND status = 'active'),
mym AS (SELECT m.unit_id, m.is_leader FROM memberships m JOIN me ON m.person_id = me.id
        WHERE m.valid_from <= :asof AND (m.valid_to IS NULL OR m.valid_to >= :asof)),
myg AS (SELECT g.group_id FROM group_members g JOIN me ON g.person_id = me.id
        WHERE g.valid_from <= :asof AND (g.valid_to IS NULL OR g.valid_to >= :asof))
SELECT r.id FROM {table} r
WHERE r.company_id = (SELECT company_id FROM me) {extra} AND (
     (r.personal AND r.owner_person_id = :me)
  OR (NOT r.personal AND r.classification = 'public')
  OR (NOT r.personal AND r.classification = 'internal' AND EXISTS (SELECT 1 FROM mym))
  OR (NOT r.personal AND r.classification = 'confidential' AND r.share_scope = 'team' AND EXISTS (
        SELECT 1 FROM mym JOIN org_unit_closure c ON c.descendant_id = mym.unit_id WHERE c.ancestor_id = r.owner_unit_id))
  OR (NOT r.personal AND r.classification = 'confidential' AND r.share_scope = 'leadership' AND EXISTS (
        SELECT 1 FROM mym JOIN org_unit_closure c ON c.ancestor_id = mym.unit_id WHERE mym.is_leader AND c.descendant_id = r.owner_unit_id))
  OR EXISTS (SELECT 1 FROM grants gr WHERE gr.resource_type = '{rtype}' AND gr.resource_id = r.id
        AND gr.relation IN ('reader','editor') AND gr.valid_from <= :asof AND (gr.valid_to IS NULL OR gr.valid_to >= :asof)
        AND ((gr.subject_type='person' AND gr.subject_id = :me)
          OR (gr.subject_type='group' AND gr.subject_id IN (SELECT group_id FROM myg))
          OR (gr.subject_type='unit' AND gr.subject_id IN (SELECT unit_id FROM mym)))))
ORDER BY r.id
