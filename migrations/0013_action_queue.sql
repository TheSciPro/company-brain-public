-- 0013 approval queue: dedupe proposals and keep who proposed them (agent run or watcher rule)
ALTER TABLE actions ADD COLUMN dedupe_key TEXT;
ALTER TABLE actions ADD COLUMN proposed_by TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE actions ADD COLUMN title TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_dedupe ON actions (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_approvals_approver ON approvals (approver_id, status);
