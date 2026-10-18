-- 0001 core schema (onboarding states + heartbeat job kinds)
-- Northwind Brain relational schema (source of truth). Portable: PostgreSQL 14+ and SQLite 3.35+.
-- Types: TEXT ids (e.g. 'NW1103', uuid strings), TIMESTAMP/DATE (ISO text in SQLite), TEXT json (JSONB in PG optional).
-- ============ ORG ============
CREATE TABLE companies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE org_units (                       -- departments AND teams (and sub-teams) in one table
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  kind TEXT NOT NULL CHECK (kind IN ('department','team')),
  name TEXT NOT NULL, parent_id TEXT REFERENCES org_units(id),   -- NULL = top-level department
  dept_id TEXT REFERENCES org_units(id),                          -- denormalised owning department
  valid_from DATE NOT NULL DEFAULT CURRENT_DATE, valid_to DATE,
  src_id TEXT);
CREATE TABLE org_unit_closure (                 -- every ancestor/descendant pair incl. self (depth 0)
  ancestor_id TEXT NOT NULL REFERENCES org_units(id),
  descendant_id TEXT NOT NULL REFERENCES org_units(id),
  depth INTEGER NOT NULL, PRIMARY KEY (ancestor_id, descendant_id));
CREATE TABLE people (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  name TEXT NOT NULL, email TEXT UNIQUE, title TEXT, grade TEXT, location TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','left','suspended')),
  external BOOLEAN NOT NULL DEFAULT FALSE, hired_on DATE, left_on DATE, src_id TEXT);
CREATE TABLE memberships (                      -- dated person -> team/department
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  unit_id TEXT NOT NULL REFERENCES org_units(id), role TEXT,
  is_leader BOOLEAN NOT NULL DEFAULT FALSE, is_primary BOOLEAN NOT NULL DEFAULT TRUE,
  valid_from DATE NOT NULL, valid_to DATE, src_id TEXT,
  CHECK (valid_to IS NULL OR valid_to >= valid_from));
CREATE TABLE reporting_lines (
  person_id TEXT NOT NULL REFERENCES people(id), manager_id TEXT NOT NULL REFERENCES people(id),
  valid_from DATE NOT NULL, valid_to DATE, src_id TEXT, PRIMARY KEY (person_id, valid_from));
CREATE TABLE groups (                           -- SCIM / ACL groups (grp-finance, grp-acc-brm, auditors)
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL, src_id TEXT);
CREATE TABLE group_members (
  group_id TEXT NOT NULL REFERENCES groups(id), person_id TEXT NOT NULL REFERENCES people(id),
  valid_from DATE NOT NULL, valid_to DATE, PRIMARY KEY (group_id, person_id, valid_from));
-- ============ ACCESS ============
CREATE TABLE classification_levels (
  code TEXT PRIMARY KEY, rank INTEGER NOT NULL UNIQUE, description TEXT NOT NULL);
INSERT INTO classification_levels VALUES
 ('public',1,'Anyone, incl. released compliance docs'),
 ('internal',2,'Any current employee'),
 ('confidential',3,'Owning team or leadership per sharing rule'),
 ('highly_confidential',4,'Named people/groups only (grants)');
CREATE TABLE access_rules (                     -- maps source ACL tags to level + sharing rule
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  source_tag TEXT NOT NULL,                     -- e.g. PUBLIC-INT, MGR, RESTRICTED-FIN
  classification TEXT NOT NULL REFERENCES classification_levels(code),
  share_scope TEXT CHECK (share_scope IN ('team','leadership')),
  UNIQUE (company_id, source_tag));
CREATE TABLE grants (                           -- explicit exceptions (ReBAC-style tuple: subject -relation-> resource)
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  subject_type TEXT NOT NULL CHECK (subject_type IN ('person','group','unit')),
  subject_id TEXT NOT NULL,
  resource_type TEXT NOT NULL CHECK (resource_type IN ('record','document','project')),
  resource_id TEXT NOT NULL,
  relation TEXT NOT NULL DEFAULT 'reader' CHECK (relation IN ('reader','existence','editor')),
  valid_from DATE NOT NULL, valid_to DATE, granted_by TEXT REFERENCES people(id), reason TEXT);
-- ============ DATA ============
CREATE TABLE documents (
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  title TEXT NOT NULL, doc_type TEXT, version INTEGER NOT NULL DEFAULT 1,
  owner_person_id TEXT REFERENCES people(id), owner_unit_id TEXT REFERENCES org_units(id),
  dept_id TEXT REFERENCES org_units(id),
  classification TEXT NOT NULL REFERENCES classification_levels(code),
  share_scope TEXT CHECK (share_scope IN ('team','leadership')), source_tag TEXT,
  personal BOOLEAN NOT NULL DEFAULT FALSE,      -- connected-app data owned by one person
  blob_url TEXT NOT NULL, mime TEXT, bytes INTEGER, hash TEXT NOT NULL,
  source TEXT, source_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, deleted_at TIMESTAMP,
  CHECK (classification <> 'confidential' OR share_scope IS NOT NULL),
  UNIQUE (company_id, source, source_id, version));
CREATE TABLE records (                          -- structured rows from any source (HR case, approval, ticket, commit...)
  id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
  source TEXT NOT NULL, source_id TEXT NOT NULL, record_type TEXT NOT NULL,
  owner_person_id TEXT REFERENCES people(id), owner_unit_id TEXT REFERENCES org_units(id),
  dept_id TEXT REFERENCES org_units(id),
  classification TEXT NOT NULL REFERENCES classification_levels(code),
  share_scope TEXT CHECK (share_scope IN ('team','leadership')), source_tag TEXT,
  personal BOOLEAN NOT NULL DEFAULT FALSE,
  payload TEXT NOT NULL, hash TEXT NOT NULL, occurred_at TIMESTAMP,
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CHECK (classification <> 'confidential' OR share_scope IS NOT NULL),
  UNIQUE (company_id, source, source_id));
-- ============ USER-SCOPED ============
CREATE TABLE user_identities (                  -- login identity -> person
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  provider TEXT NOT NULL, subject TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (provider, subject));
CREATE TABLE connected_accounts (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  provider TEXT NOT NULL DEFAULT 'composio', toolkit TEXT NOT NULL,   -- slack, gmail, github...
  external_account_id TEXT NOT NULL,            -- Composio connected_account id (no tokens stored here)
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','error')),
  default_classification TEXT NOT NULL DEFAULT 'highly_confidential' REFERENCES classification_levels(code),
  last_synced_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, revoked_at TIMESTAMP);
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  asof DATE, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ended_at TIMESTAMP,
  notes_status TEXT NOT NULL DEFAULT 'pending' CHECK (notes_status IN ('pending','running','done','failed')));
CREATE TABLE messages (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  person_id TEXT NOT NULL REFERENCES people(id),
  role TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
  content TEXT NOT NULL, hash TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE agent_runs (                       -- one agent invocation per user turn
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  person_id TEXT NOT NULL REFERENCES people(id), trigger_message_id TEXT REFERENCES messages(id),
  status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','failed','interrupted')),
  answer_kind TEXT CHECK (answer_kind IN ('answered','from_memory','not_in_graph','unknown_entity','forbidden_exists','blocked')),
  started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, finished_at TIMESTAMP, tokens INTEGER, error TEXT);
CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL REFERENCES agent_runs(id),
  tool TEXT NOT NULL, args TEXT, input_hash TEXT, result_rows INTEGER, refusal TEXT, ms INTEGER,
  status TEXT CHECK (status IN ('ok','error','denied','interrupted')),
  side_effect TEXT CHECK (side_effect IN ('none','write_memory','write_action','external')), agent_id TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE actions (                          -- agent actions needing policy/approval; mirrored as Decision in graph
  id TEXT PRIMARY KEY, agent_run_id TEXT REFERENCES agent_runs(id),
  person_id TEXT NOT NULL REFERENCES people(id), action_type TEXT NOT NULL, args TEXT,
  status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','blocked','awaiting_approval','approved','rejected','done','failed')),
  policy_ref TEXT, decision_node_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, done_at TIMESTAMP);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id),
  approver_id TEXT NOT NULL REFERENCES people(id), step INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','expired')),
  comment TEXT, decided_at TIMESTAMP);
CREATE TABLE memories (
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  kind TEXT NOT NULL CHECK (kind IN ('fact','preference','relationship','goal','credential','link','file','correction','outcome')),
  tags TEXT, people TEXT, mode TEXT CHECK (mode IN ('verbatim','summary')), pinned BOOLEAN NOT NULL DEFAULT FALSE,
  sensitivity TEXT NOT NULL DEFAULT 'highly_confidential', capture_intent TEXT,
  content TEXT NOT NULL, about_ref TEXT,         -- graph node id it is about
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','hidden')),
  version INTEGER NOT NULL DEFAULT 1, confidence REAL, src_message_id TEXT REFERENCES messages(id),
  src_record_id TEXT REFERENCES records(id), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE memory_versions (
  memory_id TEXT NOT NULL REFERENCES memories(id), version INTEGER NOT NULL,
  content TEXT NOT NULL, event TEXT NOT NULL CHECK (event IN ('ADD','UPDATE','DELETE')),
  changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reason TEXT, PRIMARY KEY (memory_id, version));
CREATE TABLE user_profiles (
  person_id TEXT PRIMARY KEY REFERENCES people(id), content TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  onboarding_state TEXT NOT NULL DEFAULT 'pending' CHECK (onboarding_state IN ('pending','p0','p1','p2','p3','p4','graduated','paused')),
  CHECK (length(content) <= 1500));
CREATE TABLE user_profile_versions (
  person_id TEXT NOT NULL REFERENCES people(id), version INTEGER NOT NULL, content TEXT NOT NULL,
  changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, reason TEXT, PRIMARY KEY (person_id, version));
CREATE TABLE session_notes (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
  person_id TEXT NOT NULL REFERENCES people(id), summary TEXT NOT NULL, topics TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
-- ============ SYSTEM ============
CREATE TABLE jobs (                             -- job / feature definitions
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL
    CHECK (kind IN ('ingest','post_session','inference','notes','watcher','expiry','graph_sync','connector_sync','heartbeat','onboarding')),
  schedule TEXT, enabled BOOLEAN NOT NULL DEFAULT TRUE, config TEXT);
CREATE TABLE job_runs (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
  scope_type TEXT, scope_id TEXT,               -- e.g. person / session / connector
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','skipped')),
  is_done BOOLEAN NOT NULL DEFAULT FALSE, attempts INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMP, finished_at TIMESTAMP, items_in INTEGER, items_out INTEGER, error TEXT,
  idempotency_key TEXT UNIQUE);
CREATE TABLE connector_events (
  id TEXT PRIMARY KEY, connected_account_id TEXT REFERENCES connected_accounts(id),
  connector TEXT NOT NULL, toolkit TEXT, trigger TEXT, external_event_id TEXT,
  payload TEXT NOT NULL, received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  processed BOOLEAN NOT NULL DEFAULT FALSE, processed_at TIMESTAMP, error TEXT,
  UNIQUE (connector, external_event_id));
CREATE TABLE graph_outbox (                     -- transactional outbox: SQL commit => graph sync later
  id INTEGER PRIMARY KEY, aggregate TEXT NOT NULL, aggregate_id TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('upsert','close','hide')), payload TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, processed_at TIMESTAMP,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY, actor_type TEXT NOT NULL CHECK (actor_type IN ('person','agent','system')),
  actor_id TEXT, on_behalf_of TEXT REFERENCES people(id), event TEXT NOT NULL,
  target_type TEXT, target_id TEXT, detail TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
-- ============ INDEXES ============
CREATE INDEX ix_units_parent ON org_units(parent_id);
CREATE INDEX ix_closure_desc ON org_unit_closure(descendant_id, ancestor_id);
CREATE INDEX ix_mem_person ON memberships(person_id, valid_from, valid_to);
CREATE INDEX ix_mem_unit ON memberships(unit_id) ;
CREATE INDEX ix_gm_person ON group_members(person_id);
CREATE INDEX ix_grants_subject ON grants(subject_type, subject_id);
CREATE INDEX ix_grants_resource ON grants(resource_type, resource_id);
CREATE INDEX ix_docs_scope ON documents(company_id, owner_unit_id, classification);
CREATE INDEX ix_docs_owner ON documents(owner_person_id);
CREATE INDEX ix_docs_hash ON documents(hash);
CREATE INDEX ix_rec_scope ON records(company_id, owner_unit_id, classification);
CREATE INDEX ix_rec_owner ON records(owner_person_id);
CREATE INDEX ix_msg_session ON messages(session_id, created_at);
CREATE INDEX ix_runs_session ON agent_runs(session_id);
CREATE INDEX ix_tc_run ON tool_calls(agent_run_id);
CREATE INDEX ix_mems_person ON memories(person_id, status);
CREATE INDEX ix_jobruns_status ON job_runs(job_id, status);
CREATE INDEX ix_outbox_pending ON graph_outbox(processed_at, id);
CREATE INDEX ix_ce_pending ON connector_events(processed, received_at);
CREATE INDEX ix_audit_target ON audit_log(target_type, target_id);
