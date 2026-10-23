"""Usage metrics for running servers and databases (WBS CC-11, CC-13).

- ``resource.resource_type`` also allows ``rds_instance``.
- ``resource_metric``: one row per resource, metric and UTC day, with the 24 hourly values
  CloudWatch returned (NULL for an hour without data). Rows older than 35 days are removed.
- ``aws_api_usage``: billable AWS API units per tenant and day (GetMetricData metrics), so
  each tenant stays inside its daily budget.

Revision ID: 0012
Revises: 0011
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
ALTER TABLE resource DROP CONSTRAINT resource_resource_type_check;
ALTER TABLE resource ADD CONSTRAINT resource_resource_type_check CHECK (resource_type IN
  ('ec2_instance', 'ebs_volume', 'ebs_snapshot', 'eip', 'elb', 'rds_instance'));

CREATE TABLE resource_metric (
  tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  resource_id uuid NOT NULL REFERENCES resource(id) ON DELETE CASCADE,
  metric      text NOT NULL,
  day         date NOT NULL,
  hourly      real[] NOT NULL CHECK (cardinality(hourly) = 24),
  fetched_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (resource_id, metric, day)
);
CREATE INDEX ON resource_metric (tenant_id, day);

CREATE TABLE aws_api_usage (
  tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  day       date NOT NULL,
  api       text NOT NULL,
  units     bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, day, api)
);

ALTER TABLE resource_metric ENABLE ROW LEVEL SECURITY;
ALTER TABLE resource_metric FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON resource_metric
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON resource_metric TO {APP_ROLE};

ALTER TABLE aws_api_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE aws_api_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON aws_api_usage
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON aws_api_usage TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS aws_api_usage;
DROP TABLE IF EXISTS resource_metric;
DELETE FROM resource WHERE resource_type = 'rds_instance';
ALTER TABLE resource DROP CONSTRAINT resource_resource_type_check;
ALTER TABLE resource ADD CONSTRAINT resource_resource_type_check CHECK (resource_type IN
  ('ec2_instance', 'ebs_volume', 'ebs_snapshot', 'eip', 'elb'));
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
