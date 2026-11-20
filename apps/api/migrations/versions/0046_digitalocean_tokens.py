"""DigitalOcean connections keep their token in the connection secret column (ADR-021).

``cloud_connection.external_id_enc`` holds each connection's secret, encrypted and bound to
the tenant (ADR-007): the External ID for AWS, the customer's read-only API token for
DigitalOcean. A DigitalOcean token is wiped on disconnect, so the column may now be NULL,
but only for a disconnected connection of a provider other than AWS: AWS rows, and every
connection still in use, keep a secret.

The downgrade refuses to run while a wiped row exists (put NOT NULL back by deleting those
connections, or their data, first).

Revision ID: 0046
Revises: 0044
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0046"
down_revision = "0044"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE cloud_connection ALTER COLUMN external_id_enc DROP NOT NULL;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_secret_check
  CHECK (external_id_enc IS NOT NULL OR (provider <> 'aws' AND status = 'disconnected'));
"""

DOWNGRADE = """
DO $$
BEGIN
  ALTER TABLE cloud_connection NO FORCE ROW LEVEL SECURITY;
  IF EXISTS (SELECT 1 FROM cloud_connection WHERE external_id_enc IS NULL) THEN
    ALTER TABLE cloud_connection FORCE ROW LEVEL SECURITY;
    RAISE EXCEPTION 'cloud_connection has disconnected connections without a secret';
  END IF;
  ALTER TABLE cloud_connection FORCE ROW LEVEL SECURITY;
END $$;
ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_secret_check;
ALTER TABLE cloud_connection ALTER COLUMN external_id_enc SET NOT NULL;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
