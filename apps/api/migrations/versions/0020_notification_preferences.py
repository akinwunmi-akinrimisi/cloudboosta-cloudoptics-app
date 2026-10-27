"""Notification preferences (PRD FR-NOT-03, WBS ADM-8).

- ``notification_preference``: whether a kind of email goes to a member. A row with
  ``user_id`` NULL is the workspace default an admin sets; a member's own row overrides
  it. With no row at all a kind is on. Kinds a member can turn off: ``weekly_digest`` and
  ``anomaly_alert``. Connection alerts (admins), budget alerts (sent to the people each
  budget names) and invites are not optional.

Whether a week's digest went to someone is the ``notification`` row
``digest:<week start>:<email>``.

Revision ID: 0020
Revises: 0019
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE notification_preference (
  tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  user_id    uuid REFERENCES app_user(id) ON DELETE CASCADE,
  kind       text NOT NULL CHECK (kind IN ('weekly_digest', 'anomaly_alert')),
  email      boolean NOT NULL,
  updated_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX notification_preference_scope ON notification_preference
  (tenant_id, coalesce(user_id, '00000000-0000-0000-0000-000000000000'::uuid), kind);

ALTER TABLE notification_preference ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_preference FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON notification_preference
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON notification_preference TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS notification_preference CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
