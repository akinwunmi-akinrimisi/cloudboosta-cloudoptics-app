"""Provisional month-to-date costs from Cost Explorer (WBS CC-9, docs/11c §1.3).

One row per connection and month: shown until the month's first Cost and Usage Report is
loaded, then ignored (the CUR is the source of truth).

Revision ID: 0004
Revises: 0003
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE cost_snapshot_ce (
  tenant_id           uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id       uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  period_start        date NOT NULL,
  through_date        date NOT NULL,
  effective_cost_usd  numeric(18, 6) NOT NULL,
  billed_cost_usd     numeric(18, 6) NOT NULL,
  credits_usd         numeric(18, 6) NOT NULL,
  daily               jsonb NOT NULL DEFAULT '[]',
  by_service          jsonb NOT NULL DEFAULT '[]',
  fetched_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, period_start)
);
CREATE INDEX ON cost_snapshot_ce (tenant_id, period_start);

ALTER TABLE cost_snapshot_ce ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_snapshot_ce FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cost_snapshot_ce
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON cost_snapshot_ce TO {APP_ROLE};
"""

DOWNGRADE = "DROP TABLE IF EXISTS cost_snapshot_ce CASCADE;"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
