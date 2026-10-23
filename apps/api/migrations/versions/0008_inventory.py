"""Resource inventory (WBS CC-10, docs/09 §2.2 "Inventory").

One row per resource seen by a scan of an account and region. A resource missing from a
later complete scan of its region gets ``deleted_at``; it reappears if seen again.

Revision ID: 0008
Revises: 0007
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE resource (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  account_id    char(12) NOT NULL,
  region_id     text NOT NULL,
  resource_type text NOT NULL CHECK (resource_type IN ('ec2_instance', 'ebs_volume',
                'ebs_snapshot', 'eip', 'elb')),
  resource_id   text NOT NULL,
  arn           text,
  name          text,
  state         text,
  config        jsonb NOT NULL DEFAULT '{{}}',
  tags          jsonb NOT NULL DEFAULT '{{}}',
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  UNIQUE (tenant_id, account_id, region_id, resource_id)
);
CREATE INDEX ON resource (tenant_id, resource_type) WHERE deleted_at IS NULL;

ALTER TABLE resource ENABLE ROW LEVEL SECURITY;
ALTER TABLE resource FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON resource
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON resource TO {APP_ROLE};
"""

DOWNGRADE = "DROP TABLE IF EXISTS resource CASCADE;"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
