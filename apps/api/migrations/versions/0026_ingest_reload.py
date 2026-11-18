"""Reload bills: load an export version again (docs/11c §1.1, "Reload bills").

``ingest_batch`` was unique per (connection, billing period, export version), so a version
could be loaded only once. An admin reload loads the latest version of a period again (for
example to fill ``cost_resource_daily`` for months loaded before it existed), so:

- ``reload``: the batch came from a reload. The first load of a version stays unique
  (``ingest_batch_first_load``); reloads add rows of their own, so the history keeps the
  original load (``superseded`` once a reload replaces it) and every reload after it.
- ``billing_file`` rows stay unique per object and point at the latest batch that read them.

Revision ID: 0026
Revises: 0025
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE ingest_batch ADD COLUMN reload boolean NOT NULL DEFAULT false;
ALTER TABLE ingest_batch
  DROP CONSTRAINT ingest_batch_connection_id_billing_period_export_version_key;
CREATE UNIQUE INDEX ingest_batch_first_load
  ON ingest_batch (connection_id, billing_period, export_version) WHERE NOT reload;
CREATE INDEX ingest_batch_period ON ingest_batch (connection_id, billing_period, created_at DESC);
"""

# Back to one row per version: files go back to the first load, which is live again if the
# reload was; the reload rows go.
DOWNGRADE = """
ALTER TABLE ingest_batch NO FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_file NO FORCE ROW LEVEL SECURITY;
UPDATE billing_file f SET batch_id = b.id
  FROM ingest_batch r JOIN ingest_batch b
    ON b.connection_id = r.connection_id AND b.billing_period = r.billing_period
   AND b.export_version = r.export_version AND NOT b.reload
 WHERE f.batch_id = r.id AND r.reload;
UPDATE ingest_batch b SET status = 'succeeded'
 WHERE NOT b.reload AND b.status = 'superseded' AND EXISTS (
   SELECT 1 FROM ingest_batch r WHERE r.reload AND r.status = 'succeeded'
      AND r.connection_id = b.connection_id AND r.billing_period = b.billing_period
      AND r.export_version = b.export_version);
DELETE FROM ingest_batch WHERE reload;
ALTER TABLE ingest_batch FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_file FORCE ROW LEVEL SECURITY;
DROP INDEX IF EXISTS ingest_batch_period;
DROP INDEX IF EXISTS ingest_batch_first_load;
ALTER TABLE ingest_batch ADD CONSTRAINT
  ingest_batch_connection_id_billing_period_export_version_key
  UNIQUE (connection_id, billing_period, export_version);
ALTER TABLE ingest_batch DROP COLUMN reload;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
