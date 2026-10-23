"""Reconciliation of loaded billing data against AWS's own totals (WBS IN-6, FR-ING-07).

One row per connection and billing period: the latest check of the CUR totals in
``cost_daily`` against Cost Explorer for the same days, overall and per account.

Revision ID: 0007
Revises: 0006
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE cost_reconciliation (
  tenant_id        uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id    uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  billing_period   date NOT NULL,
  export_version   text NOT NULL,
  through_date     date NOT NULL,
  status           text NOT NULL CHECK (status IN ('ok', 'mismatch', 'failed')),
  cur_total_usd    numeric(18, 6),
  reference_usd    numeric(18, 6),
  difference_pct   numeric(9, 4),
  accounts         jsonb NOT NULL DEFAULT '[]',
  error_code       text,
  checked_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, billing_period)
);
CREATE INDEX ON cost_reconciliation (tenant_id, billing_period);

ALTER TABLE cost_reconciliation ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_reconciliation FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cost_reconciliation
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON cost_reconciliation TO {APP_ROLE};
"""

DOWNGRADE = "DROP TABLE IF EXISTS cost_reconciliation CASCADE;"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
