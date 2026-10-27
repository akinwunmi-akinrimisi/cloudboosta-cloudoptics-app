"""Monthly executive report (PRD US-7.1, WBS ADM-9).

- ``monthly_report``: one stored report per workspace and month (``month`` is its first
  day), the figures as they were when it was made, so a forwarded report never changes.
- ``report_setting``: extra addresses that get the report besides the members.
- ``notification_preference`` gains the kind ``monthly_report`` (members can turn it off).

Whether a report went to someone is the ``notification`` row ``report:<YYYY-MM>:<email>``.

Revision ID: 0021
Revises: 0020
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"
TENANT_TABLES = ("monthly_report", "report_setting")

UPGRADE = """
CREATE TABLE monthly_report (
  tenant_id    uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  month        date NOT NULL CHECK (extract(day FROM month) = 1),
  content      jsonb NOT NULL,
  generated_at timestamptz NOT NULL DEFAULT now(),
  generated_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  PRIMARY KEY (tenant_id, month)
);

CREATE TABLE report_setting (
  tenant_id  uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
  emails     text[] NOT NULL DEFAULT '{}',
  updated_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE notification_preference DROP CONSTRAINT notification_preference_kind_check;
ALTER TABLE notification_preference ADD CONSTRAINT notification_preference_kind_check
  CHECK (kind IN ('weekly_digest', 'anomaly_alert', 'monthly_report'));
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
DELETE FROM notification_preference WHERE kind = 'monthly_report';
ALTER TABLE notification_preference DROP CONSTRAINT notification_preference_kind_check;
ALTER TABLE notification_preference ADD CONSTRAINT notification_preference_kind_check
  CHECK (kind IN ('weekly_digest', 'anomaly_alert'));
DROP TABLE IF EXISTS report_setting, monthly_report CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
