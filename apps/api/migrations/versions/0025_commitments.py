"""Savings Plans and Reserved Instances (PRD US-4.5, FR-ANA-10, docs/09 "commitments").

- ``commitment``: the Savings Plans and Reserved Instances (EC2 and RDS) a connection's
  accounts hold, as last listed by the daily refresh, with their 30-day utilisation from
  Cost Explorer. One row per commitment; ``last_seen_at`` says when the refresh last found
  it (a commitment no longer listed is kept for the history and shown as gone).
- ``commitment_snapshot``: the Cost Explorer answers behind the Savings Plan advisory, one
  row per connection (replaced on each refresh): AWS's purchase recommendations, coverage
  and utilisation.
- ``notification_preference`` gains the kind ``commitment_expiry`` (members can turn the
  60/30/15-day expiry emails off). Whether a commitment's alert for a threshold went out is
  the ``notification`` row ``commitment:<commitment row id>:<days>``.

Revision ID: 0025
Revises: 0024
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"
TENANT_TABLES = ("commitment", "commitment_snapshot")

UPGRADE = """
CREATE TABLE commitment (
  id                    uuid PRIMARY KEY,
  tenant_id             uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id         uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  kind                  text NOT NULL
                        CHECK (kind IN ('savings_plan', 'ec2_reserved', 'rds_reserved')),
  commitment_id         text NOT NULL,
  account_id            char(12),
  region_id             text,
  description           text NOT NULL,
  plan_type             text,
  term_months           smallint,
  payment_option        text,
  state                 text,
  quantity              integer,
  hourly_commitment_usd numeric(18, 6),
  upfront_usd           numeric(18, 2),
  starts_at             timestamptz,
  ends_at               timestamptz,
  utilisation_pct       numeric(6, 2),
  details               jsonb NOT NULL DEFAULT '{}',
  first_seen_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, connection_id, kind, commitment_id)
);
CREATE INDEX ON commitment (tenant_id, ends_at);

CREATE TABLE commitment_snapshot (
  tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id   uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  recommendations jsonb NOT NULL DEFAULT '[]',
  coverage        jsonb NOT NULL DEFAULT '{}',
  utilisation     jsonb NOT NULL DEFAULT '{}',
  errors          jsonb NOT NULL DEFAULT '[]',
  fetched_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id)
);

ALTER TABLE notification_preference DROP CONSTRAINT notification_preference_kind_check;
ALTER TABLE notification_preference ADD CONSTRAINT notification_preference_kind_check
  CHECK (kind IN ('weekly_digest', 'anomaly_alert', 'monthly_report', 'commitment_expiry'));
"""

for _table in TENANT_TABLES:
    UPGRADE += f"""
ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {_table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {_table}
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON {_table} TO {APP_ROLE};
"""

DOWNGRADE = """
DELETE FROM notification_preference WHERE kind = 'commitment_expiry';
ALTER TABLE notification_preference DROP CONSTRAINT notification_preference_kind_check;
ALTER TABLE notification_preference ADD CONSTRAINT notification_preference_kind_check
  CHECK (kind IN ('weekly_digest', 'anomaly_alert', 'monthly_report'));
DROP TABLE IF EXISTS commitment_snapshot CASCADE;
DROP TABLE IF EXISTS commitment CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
