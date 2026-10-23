"""Connection health and email notifications (PRD FR-CON-09, FR-NOT-01).

- ``cloud_connection.health``: the result of the last completed validation (healthy,
  degraded or failed). ``status`` also shows ``validating`` while a check runs, so alerts
  compare against ``health`` instead. Null until the first validation.
- ``notification``: one row per email sent or attempted, for de-duplication and support.

Revision ID: 0010
Revises: 0009
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
ALTER TABLE cloud_connection ADD COLUMN health text
  CHECK (health IN ('healthy', 'degraded', 'failed'));
UPDATE cloud_connection SET health = status WHERE status IN ('healthy', 'degraded', 'failed');

CREATE TABLE notification (
  id         uuid PRIMARY KEY,
  tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  kind       text NOT NULL,
  dedupe_key text NOT NULL,
  recipients text[] NOT NULL,
  subject    text NOT NULL,
  status     text NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')),
  error      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, dedupe_key)
);

ALTER TABLE notification ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON notification
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON notification TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS notification CASCADE;
ALTER TABLE cloud_connection DROP COLUMN IF EXISTS health;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
