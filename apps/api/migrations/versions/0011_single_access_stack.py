"""The single Cloudoptics stack (cloudoptics-access.yaml).

- ``connection_intent``: a one-click link made before the AWS account is known. The stack's
  setup step calls back with the account, and the intent becomes a ``cloud_connection``.
- ``cloud_connection.stack_version``: the template version the stack last reported (NULL for
  connections still on the older billing and resource stacks).

Revision ID: 0011
Revises: 0010
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
ALTER TABLE cloud_connection ADD COLUMN stack_version text;

CREATE TABLE connection_intent (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  external_id_enc bytea NOT NULL,
  connection_id   uuid REFERENCES cloud_connection(id) ON DELETE CASCADE,
  error           text,
  created_by      uuid REFERENCES app_user(id),
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON connection_intent (tenant_id);

ALTER TABLE connection_intent ENABLE ROW LEVEL SECURITY;
ALTER TABLE connection_intent FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON connection_intent
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON connection_intent TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS connection_intent CASCADE;
ALTER TABLE cloud_connection DROP COLUMN IF EXISTS stack_version;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
