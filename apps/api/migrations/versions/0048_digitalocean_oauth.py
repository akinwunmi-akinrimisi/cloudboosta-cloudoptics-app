"""DigitalOcean sign-in (OAuth, ADR-021): how a connection's secret was obtained, when its
access token expires, and the short-lived sign-in states.

- ``cloud_connection.credential_type``: ``static`` (an External ID, a pasted token, an
  ownership code: everything before this migration) or ``oauth`` (DigitalOcean's access and
  refresh tokens, kept together in ``external_id_enc``, encrypted and tenant-bound as before).
- ``cloud_connection.token_expires_at``: when the stored access token stops working; the
  hourly renewal job reads it. NULL for static secrets.
- ``oauth_state``: one row per "Connect DigitalOcean" click. The browser carries only the
  random ``state``; the row keeps its SHA-256, the workspace and user that started it, the
  PKCE verifier (encrypted, tenant-bound) and a 10-minute expiry. The callback marks it used,
  so each state works once, for that user in that workspace. RLS: ENABLE + FORCE with the
  tenant policy, like every tenant table.

Revision ID: 0048
Revises: 0047
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
ALTER TABLE cloud_connection
  ADD COLUMN credential_type text NOT NULL DEFAULT 'static'
    CHECK (credential_type IN ('static', 'oauth')),
  ADD COLUMN token_expires_at timestamptz;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_oauth_check
  CHECK (credential_type = 'static' OR provider = 'digitalocean');
CREATE INDEX cloud_connection_token_expiry ON cloud_connection (token_expires_at)
  WHERE credential_type = 'oauth';

CREATE TABLE oauth_state (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  user_id       uuid NOT NULL,
  provider      text NOT NULL CHECK (provider IN ('digitalocean')),
  state_hash    bytea NOT NULL UNIQUE CHECK (length(state_hash) = 32),
  verifier_enc  bytea,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  used_at       timestamptz
);
CREATE INDEX ON oauth_state (tenant_id);

ALTER TABLE oauth_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON oauth_state
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_state TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS oauth_state CASCADE;
DROP INDEX IF EXISTS cloud_connection_token_expiry;
ALTER TABLE cloud_connection DROP CONSTRAINT IF EXISTS cloud_connection_oauth_check;
ALTER TABLE cloud_connection DROP COLUMN IF EXISTS token_expires_at;
ALTER TABLE cloud_connection DROP COLUMN IF EXISTS credential_type;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
