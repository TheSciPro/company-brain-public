-- Cross-user jobs (consolidation, post-session, heartbeat) run on the owner URL and switch to the app role
-- with SET LOCAL ROLE nw_app before a per-person statement, so row-level security applies to them.
-- A non-superuser owner (Neon's neondb_owner) can only do that as a member of nw_app. The owner created
-- the role, so it may grant itself membership. A superuser already can, and the grant is a no-op then.
GRANT nw_app TO CURRENT_USER;
