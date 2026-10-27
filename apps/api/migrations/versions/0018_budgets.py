"""Budgets (PRD US-6.1/6.2, WBS API-6).

- ``budget``: a monthly budget for the workspace or some of its accounts, in USD or a local
  currency, with thresholds (``[{kind: actual|forecast, percent}]``) and recipients
  (``{users: [user id], emails: [address]}``). Thresholds sit in the row rather than in a
  table of their own (docs/09 §4 sketch): whether one has fired this month is the
  ``notification`` row keyed ``budget:<id>:<kind>:<percent>:<YYYY-MM>``.

Tag-scoped budgets wait for tag values in the cost aggregates (only a hash is kept today).

Revision ID: 0018
Revises: 0017
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE budget (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  scope       jsonb NOT NULL,
  amount      numeric(18, 2) NOT NULL CHECK (amount > 0),
  currency    char(3) NOT NULL,
  thresholds  jsonb NOT NULL,
  recipients  jsonb NOT NULL,
  is_active   boolean NOT NULL DEFAULT true,
  created_by  uuid REFERENCES app_user(id) ON DELETE SET NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON budget (tenant_id);

ALTER TABLE budget ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON budget
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON budget TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS budget CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
