-- 0002 new-hire onboarding
CREATE TABLE onboarding_templates (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  unit_id TEXT REFERENCES org_units(id), grade_band TEXT, role_family TEXT,
  category TEXT NOT NULL CHECK (category IN ('accounts','training','policy','people','ramp','access')),
  title TEXT NOT NULL, target_ref TEXT, due_day INTEGER NOT NULL CHECK (due_day >= 0),
  mandatory BOOLEAN NOT NULL DEFAULT TRUE, verify_rule TEXT);
CREATE TABLE onboarding_plans (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL UNIQUE REFERENCES people(id),
  hire_event_id TEXT, kind TEXT NOT NULL DEFAULT 'hire' CHECK (kind IN ('hire','transfer')),
  start_date DATE NOT NULL, phase TEXT NOT NULL DEFAULT 'p0'
    CHECK (phase IN ('p0','p1','p2','p3','p4','graduated','paused')),
  buddy_id TEXT REFERENCES people(id), welcomed_at TIMESTAMP, graduated_at TIMESTAMP,
  CHECK (buddy_id IS NULL OR buddy_id <> person_id));
CREATE TABLE onboarding_items (
  id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES onboarding_plans(id),
  person_id TEXT NOT NULL REFERENCES people(id),
  template_id TEXT REFERENCES onboarding_templates(id),
  category TEXT NOT NULL CHECK (category IN ('accounts','training','policy','people','ramp','access')),
  title TEXT NOT NULL, target_ref TEXT, due_day INTEGER NOT NULL, mandatory BOOLEAN NOT NULL DEFAULT TRUE,
  verify_rule TEXT, status TEXT NOT NULL DEFAULT 'todo'
    CHECK (status IN ('todo','doing','done','waived','blocked')),
  evidence_ref TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE policy_acks (
  person_id TEXT NOT NULL REFERENCES people(id), policy_id TEXT NOT NULL, version TEXT NOT NULL,
  acked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (person_id, policy_id, version));
CREATE TABLE intents (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  trigger_keywords TEXT NOT NULL, action TEXT NOT NULL, expires_at TIMESTAMP,
  fires_left INTEGER NOT NULL DEFAULT 1 CHECK (fires_left >= 0), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE nudges (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  item_id TEXT REFERENCES onboarding_items(id), channel TEXT NOT NULL DEFAULT 'inbox'
    CHECK (channel IN ('inbox','slack_dm')),
  sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  outcome TEXT NOT NULL DEFAULT 'shown' CHECK (outcome IN ('shown','snoozed','done','dismissed')),
  dedupe_key TEXT NOT NULL UNIQUE);
CREATE INDEX ix_obitems_person ON onboarding_items(person_id, status, due_day);
CREATE INDEX ix_nudges_person ON nudges(person_id, sent_at);
CREATE INDEX ix_intents_person ON intents(person_id);
