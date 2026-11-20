"""Azure connections (ADR-020, multi-cloud plan Phase 2b).

``azure_connection`` holds what an Azure connection needs beside its ``cloud_connection`` row
(one to one, same tenant): the customer's Entra directory (tenant) ID, the scope the costs
come from (one subscription, or a billing account), the subscriptions it covers, and where
the Cost Management FOCUS export is written (storage account, container, folder). Found on
validation: the storage account's ARM ID and the export's name.

``cloud_connection`` keeps its AWS columns for every provider (ADR-018); an Azure row sets
``provider_account_id`` to the subscription GUID (or the billing account ID),
``cur_bucket``/``cur_prefix`` to "<storage account>/<container>" and the export folder (so the
6-hourly poll picks it up), and ``external_id_enc`` to the connection's ownership code (the
tag the customer sets on the export's storage account). ``role_arn`` and ``stack_name`` are
not used ("" and "azure").

RLS: ENABLE + FORCE with the tenant policy, like every tenant table.

Revision ID: 0047
Revises: 0046
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE azure_connection (
  connection_id      uuid PRIMARY KEY REFERENCES cloud_connection(id) ON DELETE CASCADE,
  tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  directory_id       uuid NOT NULL,
  scope_type         text NOT NULL CHECK (scope_type IN ('subscription', 'billing_account')),
  billing_account_id text CHECK (billing_account_id IS NULL
                                 OR length(billing_account_id) BETWEEN 1 AND 128),
  subscription_ids   text[] NOT NULL DEFAULT '{{}}',
  storage_account    text NOT NULL CHECK (storage_account ~ '^[a-z0-9]{{3,24}}$'),
  container          text NOT NULL CHECK (container ~ '^[a-z0-9][a-z0-9-]{{1,61}}[a-z0-9]$'),
  export_path        text NOT NULL DEFAULT '' CHECK (length(export_path) <= 512),
  storage_account_id text,
  export_name        text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((scope_type = 'billing_account') = (billing_account_id IS NOT NULL))
);
CREATE INDEX ON azure_connection (tenant_id);

ALTER TABLE azure_connection ENABLE ROW LEVEL SECURITY;
ALTER TABLE azure_connection FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON azure_connection
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON azure_connection TO {APP_ROLE};
"""

DOWNGRADE = "DROP TABLE IF EXISTS azure_connection CASCADE;"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
