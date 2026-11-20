"""One Cloudoptics service account per Google Cloud connection (ADR-019, 20 Nov 2026).

- ``gcp_connection.service_account_email``: the connection's own service account in
  Cloudoptics' project (``co-<12 hex>@<project>.iam.gserviceaccount.com``), unique across all
  workspaces; ``service_account_state``: ``active``, ``delete_pending`` (Google could not be
  reached at disconnect; retried at data deletion) or ``deleted``.
- ``gcp_connection.query_project_id`` may be NULL: the queries run in Cloudoptics' project, at
  Cloudoptics' cost (no BigQuery Job User for the customer to grant).
- A Google Cloud connection keeps no secret: the dataset label is gone, so
  ``external_id_enc`` may be NULL for ``gcp`` whatever its status (the 0046 CHECK widened).
- **Migration of label-era connections**: every Google Cloud connection not yet disconnected
  is disconnected (no real customer had connected one yet). Connecting the same billing
  account again reuses the row, so its cost history stays, and gives it its own account.

Revision ID: 0049
Revises: 0048
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE gcp_connection
  ADD COLUMN service_account_email text
    CHECK (service_account_email ~ ('^[a-z][-a-z0-9]{4,28}[a-z0-9]@[a-z][-a-z0-9]{4,28}'
                                    '[a-z0-9]\.iam\.gserviceaccount\.com$')),
  ADD COLUMN service_account_state text
    CHECK (service_account_state IN ('active', 'delete_pending', 'deleted')),
  ADD CONSTRAINT gcp_connection_account_state_check
    CHECK ((service_account_email IS NULL) = (service_account_state IS NULL));
ALTER TABLE gcp_connection ALTER COLUMN query_project_id DROP NOT NULL;
CREATE UNIQUE INDEX gcp_connection_service_account_key ON gcp_connection (service_account_email)
  WHERE service_account_email IS NOT NULL;

ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_secret_check;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_secret_check
  CHECK (external_id_enc IS NOT NULL OR provider = 'gcp'
         OR (provider <> 'aws' AND status = 'disconnected'));

UPDATE cloud_connection SET status = 'disconnected', health = NULL, account_verified_at = NULL,
  external_id_enc = NULL
WHERE provider = 'gcp' AND status <> 'disconnected';
"""

DOWNGRADE = """
UPDATE cloud_connection SET status = 'disconnected', health = NULL, account_verified_at = NULL
WHERE provider = 'gcp' AND external_id_enc IS NULL AND status <> 'disconnected';
ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_secret_check;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_secret_check
  CHECK (external_id_enc IS NOT NULL OR (provider <> 'aws' AND status = 'disconnected'));
UPDATE gcp_connection SET query_project_id = export_project_id WHERE query_project_id IS NULL;
ALTER TABLE gcp_connection ALTER COLUMN query_project_id SET NOT NULL;
DROP INDEX IF EXISTS gcp_connection_service_account_key;
ALTER TABLE gcp_connection DROP CONSTRAINT IF EXISTS gcp_connection_account_state_check;
ALTER TABLE gcp_connection DROP COLUMN IF EXISTS service_account_state;
ALTER TABLE gcp_connection DROP COLUMN IF EXISTS service_account_email;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
