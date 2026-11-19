"""Data deletion requests and their verification records (WBS ADM-7, PRD US-8.3).

``data_deletion`` holds one row per request to delete a disconnected connection's data or
the whole workspace. It has no foreign key to ``tenant`` on purpose: the row outlives the
workspace it describes, so the verification (row counts per table before and after) stays
on record after everything else is gone. ``tenant_id`` still scopes it by RLS; the
scheduler policy lets the periodic job find due requests across tenants (read-only), as
``cloud_connection`` does (0003).

Deleting a connection's anomalies needs DELETE on ``anomaly`` (0019 granted no DELETE).
Everything else goes by ``ON DELETE CASCADE`` from ``cloud_connection`` or ``tenant``, or
by DELETE grants the app role already has (``recommendation``, ``tag_set``, ``tenant``).

Revision ID: 0032
Revises: 0031
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE data_deletion (
  id             uuid PRIMARY KEY,
  tenant_id      uuid NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('connection', 'tenant')),
  connection_id  uuid,
  target_label   text NOT NULL,
  status         text NOT NULL DEFAULT 'scheduled'
                 CHECK (status IN ('scheduled', 'cancelled', 'completed', 'failed')),
  requested_by   uuid,
  requested_at   timestamptz NOT NULL DEFAULT now(),
  scheduled_for  timestamptz NOT NULL,
  cancelled_by   uuid,
  cancelled_at   timestamptz,
  completed_at   timestamptz,
  verification   jsonb,
  error          text,
  CHECK ((kind = 'connection') = (connection_id IS NOT NULL))
);
CREATE INDEX ON data_deletion (tenant_id, requested_at DESC);
CREATE INDEX data_deletion_due ON data_deletion (scheduled_for) WHERE status = 'scheduled';
-- One open request per workspace, and per connection.
CREATE UNIQUE INDEX data_deletion_open_tenant ON data_deletion (tenant_id)
  WHERE kind = 'tenant' AND status = 'scheduled';
CREATE UNIQUE INDEX data_deletion_open_connection ON data_deletion (connection_id)
  WHERE kind = 'connection' AND status = 'scheduled';

ALTER TABLE data_deletion ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_deletion FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON data_deletion
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY scheduler_read ON data_deletion FOR SELECT USING (app_scheduler());
GRANT SELECT, INSERT, UPDATE ON data_deletion TO {APP_ROLE};

GRANT DELETE ON anomaly TO {APP_ROLE};
"""

DOWNGRADE = f"""
REVOKE DELETE ON anomaly FROM {APP_ROLE};
DROP TABLE IF EXISTS data_deletion;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
