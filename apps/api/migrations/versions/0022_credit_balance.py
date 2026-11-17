"""AWS credit balances (PRD US-2.4, FR-ANA-11, docs/09 ``CreditBalanceEntry``).

- ``credit_balance``: the remaining credit balance an admin read in the AWS Billing
  console on a day (no AWS API reports it, PRD Q7), with the expiry date if known.
  Append-only: the newest entry is the one used, the rest is history.

Revision ID: 0022
Revises: 0021
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE credit_balance (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  amount_usd  numeric(18, 2) NOT NULL CHECK (amount_usd >= 0),
  as_of       date NOT NULL,
  expires_on  date,
  note        text CHECK (length(note) <= 200),
  entered_by  uuid REFERENCES app_user(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON credit_balance (tenant_id, created_at DESC);

ALTER TABLE credit_balance ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_balance FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON credit_balance
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT ON credit_balance TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS credit_balance CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
