"""Stack callback lifecycle (security audit run 1, leads 2, 3, 4 and 10).

- ``connection_intent.expires_at``: a one-click link works for 7 days. Links made before this
  migration get 7 days from when they were made, so old unused links stop working now.
- ``connection_intent.revoked_at``: an admin can withdraw a link that has not been used.
- ``cloud_connection.pending_stack``: what a stack callback reported for a connection that
  already exists (role, stack, account type, export). It replaces the stored values only
  once the role it names can be assumed; until then the known-good values stay.

The app role already has SELECT, INSERT and UPDATE on both tables (0001, 0011), which cover
the new columns; revoking is an UPDATE, so no DELETE grant is needed.

Revision ID: 0028
Revises: 0027
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE connection_intent ADD COLUMN expires_at timestamptz;
ALTER TABLE connection_intent ADD COLUMN revoked_at timestamptz;
ALTER TABLE connection_intent NO FORCE ROW LEVEL SECURITY;
UPDATE connection_intent SET expires_at = created_at + interval '7 days';
ALTER TABLE connection_intent FORCE ROW LEVEL SECURITY;
ALTER TABLE connection_intent ALTER COLUMN expires_at SET DEFAULT now() + interval '7 days';
ALTER TABLE connection_intent ALTER COLUMN expires_at SET NOT NULL;

ALTER TABLE cloud_connection ADD COLUMN pending_stack jsonb;
"""

DOWNGRADE = """
ALTER TABLE cloud_connection DROP COLUMN IF EXISTS pending_stack;
ALTER TABLE connection_intent DROP COLUMN IF EXISTS revoked_at;
ALTER TABLE connection_intent DROP COLUMN IF EXISTS expires_at;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
