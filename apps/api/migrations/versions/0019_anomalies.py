"""Spend anomalies (PRD US-6.3, FR-ANA-08, WBS AN-11).

- ``anomaly``: one row per (account, service, day) whose cost is well above its baseline,
  with the expected cost, the top usage types behind it and a status a member sets
  (open / investigating / expected / resolved). A row created while the same scope was
  marked expected in the week before is stored as expected, with ``suppressed_by`` naming
  that anomaly, and sends no alert.
- ``alert_setting``: one row per tenant with its anomaly thresholds (minimum impact per day
  in USD or the workspace currency, percent above baseline) and extra alert addresses.
  Missing values take the defaults (USD 50 a day, 30%).

Whether an anomaly has been emailed is the ``notification`` row ``anomaly:<id>``.

Revision ID: 0019
Revises: 0018
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"
TENANT_TABLES = ("anomaly", "alert_setting")

UPGRADE = """
CREATE TABLE anomaly (
  id                uuid PRIMARY KEY,
  tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  usage_date        date NOT NULL,
  account_id        char(12) NOT NULL,
  service_code      text NOT NULL,
  service_name      text NOT NULL,
  actual_usd        numeric(18, 6) NOT NULL,
  expected_usd      numeric(18, 6) NOT NULL,
  mad_usd           numeric(18, 6) NOT NULL,
  impact_usd        numeric(18, 6) NOT NULL,
  contributors      jsonb NOT NULL DEFAULT '[]',
  status            text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'investigating', 'expected', 'resolved')),
  suppressed_by     uuid REFERENCES anomaly(id) ON DELETE SET NULL,
  note              text CHECK (length(note) <= 500),
  status_changed_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  status_changed_at timestamptz,
  engine_version    text NOT NULL,
  detected_at       timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, usage_date, account_id, service_code)
);
CREATE INDEX ON anomaly (tenant_id, status, usage_date);

CREATE TABLE alert_setting (
  tenant_id  uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
  settings   jsonb NOT NULL DEFAULT '{}',
  updated_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

for _table in TENANT_TABLES:
    UPGRADE += f"""
ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {_table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {_table}
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON {_table} TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS alert_setting, anomaly CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
