"""Per-resource daily costs for the billing-only rules (PRD FR-ANA-06b R6b, FR-ANA-14 R10).

- ``cost_resource_daily``: day x account x service x region x usage type x resource x tag
  set, only for the usage types those rules read (NAT gateway hours and bytes, RDS and EKS
  Extended Support; ``analysis.billing.BILLED_USAGE_TYPES``; from 0024 every resource line
  is kept). ``cost_daily`` drops the resource, so it cannot say which gateway or database
  a charge is for. Replaced with ``cost_daily`` per connection and billing period; under
  tenant row-level security.

Revision ID: 0023
Revises: 0022
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE cost_resource_daily (
  tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id      uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  usage_date         date NOT NULL,
  billing_period     date NOT NULL,
  account_id         char(12) NOT NULL,
  service_code       text NOT NULL,
  region_id          text,
  usage_type         text NOT NULL,
  resource_id        text NOT NULL,
  tag_set_hash       bytea,
  effective_cost_usd numeric(18, 6) NOT NULL,
  usage_quantity     numeric(24, 6) NOT NULL
);
CREATE INDEX ON cost_resource_daily (tenant_id, usage_date);
CREATE INDEX ON cost_resource_daily (connection_id, billing_period);

ALTER TABLE cost_resource_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_resource_daily FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cost_resource_daily
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, DELETE ON cost_resource_daily TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS cost_resource_daily CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
