"""Send the connect step to the workspace's cloud admin (connect invites, ADR-022).

- ``connect_invite``: a workspace admin sends one cloud's connect step to an email address.
  The email carries a random link token; only its SHA-256 is kept (``token_hash``). Opening
  the link starts a narrowly scoped session whose random token is kept the same way
  (``session_hash``). The invite works for 7 days, until revoked, while its sender is still an
  admin, and until it has connected one account. Kept after use for the status shown to the
  sender; the audit log keeps every step.
- ``connect_invite_lookup(link, session)``: the person holding a link is not signed in and
  not in the workspace, so before any scope is set the API needs the invite's workspace.
  SECURITY DEFINER with a fixed ``search_path``; it returns the invite and workspace IDs for a
  matching hash and nothing else, so no other row is readable without a scope.
- ``scheduler_read``: the sweep job (``connectors:connect_invites.sweep``) lists invites
  across workspaces, read-only, as the other scheduled fan-outs do (0003).
- ``audit_log.actor_type`` gains ``connect_invite``: steps taken through an invite are
  recorded as the invite (its ``actor_id``), not as a user.

RLS: ENABLE + FORCE with the tenant policy, like every tenant table.

Revision ID: 0051
Revises: 0049
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0051"
down_revision = "0049"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE connect_invite (
  id                 uuid PRIMARY KEY,
  tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  provider           text NOT NULL CHECK (provider IN ('aws', 'gcp', 'azure', 'digitalocean')),
  email              citext NOT NULL,
  token_hash         bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
  session_hash       bytea UNIQUE CHECK (length(session_hash) = 32),
  invited_by         uuid REFERENCES app_user(id) ON DELETE SET NULL,
  intent_id          uuid REFERENCES connection_intent(id) ON DELETE SET NULL,
  connection_id      uuid REFERENCES cloud_connection(id) ON DELETE CASCADE,
  email_sent         boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,
  opened_at          timestamptz,
  connected_at       timestamptz,
  revoked_at         timestamptz,
  expiry_notified_at timestamptz
);
CREATE INDEX ON connect_invite (tenant_id, created_at DESC);
CREATE INDEX connect_invite_open ON connect_invite (expires_at)
  WHERE connected_at IS NULL AND revoked_at IS NULL AND expiry_notified_at IS NULL;

ALTER TABLE connect_invite ENABLE ROW LEVEL SECURITY;
ALTER TABLE connect_invite FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connect_invite
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
-- The sweep job finds invites that connected or expired, then works in each tenant's scope.
CREATE POLICY scheduler_read ON connect_invite FOR SELECT USING (app_scheduler());
GRANT SELECT, INSERT, UPDATE ON connect_invite TO {APP_ROLE};

CREATE FUNCTION connect_invite_lookup(link bytea, sess bytea)
RETURNS TABLE (id uuid, tenant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, i.tenant_id FROM public.connect_invite i
  WHERE (link IS NOT NULL AND i.token_hash = link)
     OR (sess IS NOT NULL AND i.session_hash = sess)
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION connect_invite_lookup(bytea, bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION connect_invite_lookup(bytea, bytea) TO {APP_ROLE};

-- FORCE row-level security binds the owner too (doadmin on staging is not a superuser):
-- the function, running as the owner, must see every invite to find one by its hash.
DO $$
DECLARE tbl_owner name := (SELECT tableowner FROM pg_tables
                           WHERE schemaname = 'public' AND tablename = 'connect_invite');
BEGIN
  EXECUTE format('ALTER FUNCTION connect_invite_lookup(bytea, bytea) OWNER TO %I', tbl_owner);
  EXECUTE format('CREATE POLICY connect_invite_owner ON connect_invite FOR SELECT TO %I '
                 'USING (true)', tbl_owner);
END $$;

ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_type_check
  CHECK (actor_type IN ('user', 'system', 'staff', 'connect_invite'));
"""

DOWNGRADE = """
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_actor_type_check
  CHECK (actor_type IN ('user', 'system', 'staff')) NOT VALID;
DROP POLICY IF EXISTS connect_invite_owner ON connect_invite;
DROP FUNCTION IF EXISTS connect_invite_lookup(bytea, bytea);
DROP TABLE IF EXISTS connect_invite CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
