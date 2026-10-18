-- 0003 who an ACL tag grants to (directory/acl_classifications.json -> classifications{tag: [group|person]})
CREATE TABLE access_rule_subjects (
  rule_id TEXT NOT NULL REFERENCES access_rules(id),
  subject_type TEXT NOT NULL CHECK (subject_type IN ('person','group')),
  subject_id TEXT NOT NULL, PRIMARY KEY (rule_id, subject_type, subject_id));
CREATE INDEX ix_rp_person ON reporting_lines(manager_id);
CREATE INDEX ix_gm_group ON group_members(group_id, valid_from);
