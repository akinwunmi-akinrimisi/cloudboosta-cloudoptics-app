"""Workspace invites (WBS ADM-3, PRD US-8.1).

- ``invite``: an admin invites an email address with a role. The first time someone
  with that email, confirmed by the sign-in provider, signs in, they join the workspace.
  Invites expire after 7 days, can be revoked, and are kept after use for the audit trail.

The invitee is not in the workspace yet, so besides the usual tenant isolation a user may
see the invites addressed to their own email.

Revision ID: 0015
Revises: 0014
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE invite (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  email       citext NOT NULL,
  role        text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
  invited_by  uuid REFERENCES app_user(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  accepted_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  revoked_at  timestamptz
);
-- At most one open invitation per address and workspace.
CREATE UNIQUE INDEX invite_open ON invite (tenant_id, email)
  WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX ON invite (email) WHERE accepted_at IS NULL AND revoked_at IS NULL;

ALTER TABLE invite ENABLE ROW LEVEL SECURITY;
ALTER TABLE invite FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invite
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY addressed_to_me ON invite FOR SELECT
  USING (email = (SELECT u.email FROM app_user u WHERE u.id = app_current_user()));
GRANT SELECT, INSERT, UPDATE ON invite TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS invite;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
