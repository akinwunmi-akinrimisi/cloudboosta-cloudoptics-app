"""Google Cloud connections (ADR-019, multi-cloud plan Phase 2).

- ``gcp_connection``: where a Google Cloud connection's bill is, one row per
  ``cloud_connection`` with ``provider = 'gcp'``: the Cloud Billing account, the BigQuery
  table or view holding the FOCUS rows (the managed FOCUS export, or the detailed export with
  Google's FOCUS view), the project that runs (and pays for) the queries, and the dataset's
  location. The connection itself keeps the billing account as ``provider_account_id``, so the
  one-verified-workspace-per-account rule (migration 0017) holds per billing account; projects
  are its accounts (``cloud_account``).

Nothing in it is secret: the ownership label value is the connection's ``external_id_enc``.
Tenant-isolated like every workspace table (FORCE RLS); removed with its connection.

Revision ID: 0044
Revises: 0043
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE gcp_connection (
  connection_id      uuid PRIMARY KEY REFERENCES cloud_connection(id) ON DELETE CASCADE,
  tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  billing_account_id text NOT NULL
                     CHECK (billing_account_id ~ '^[0-9A-F]{{6}}-[0-9A-F]{{6}}-[0-9A-F]{{6}}$'),
  export_project_id  text NOT NULL CHECK (export_project_id ~ '^[a-z][a-z0-9-]{{4,28}}[a-z0-9]$'),
  export_dataset_id  text NOT NULL CHECK (export_dataset_id ~ '^[A-Za-z0-9_]+$'
                     AND length(export_dataset_id) <= 1024),
  export_table_id    text NOT NULL CHECK (export_table_id ~ '^[A-Za-z0-9_-]+$'
                     AND length(export_table_id) <= 1024),
  export_kind        text NOT NULL CHECK (export_kind IN ('focus_export', 'focus_view')),
  query_project_id   text NOT NULL CHECK (query_project_id ~ '^[a-z][a-z0-9-]{{4,28}}[a-z0-9]$'),
  dataset_location   text CHECK (dataset_location ~ '^[A-Za-z0-9-]{{2,40}}$'),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON gcp_connection (tenant_id);

ALTER TABLE gcp_connection ENABLE ROW LEVEL SECURITY;
ALTER TABLE gcp_connection FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON gcp_connection
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON gcp_connection TO {APP_ROLE};
"""

DOWNGRADE = "DROP TABLE IF EXISTS gcp_connection CASCADE;"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
