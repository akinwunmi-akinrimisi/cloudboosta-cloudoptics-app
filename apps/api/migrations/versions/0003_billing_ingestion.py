"""Billing ingestion: export versions, billing files, tag sets and daily cost aggregates.

``cost_daily`` is a plain table for now; monthly partitions (docs/09 §2.2) come when volume
needs them. The scheduler policy lets the periodic CUR poll list connections across tenants
(read-only, ``cloud_connection`` only), see ``cloudoptics.platform.db.scheduler_scope``.

Revision ID: 0003
Revises: 0002
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"
TENANT_TABLES = ("ingest_batch", "billing_file", "tag_set", "cost_daily")

UPGRADE = f"""
CREATE TABLE ingest_batch (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id   uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  billing_period  date NOT NULL,
  export_version  text NOT NULL,
  manifest_key    text NOT NULL,
  mapping_version text NOT NULL,
  status          text NOT NULL CHECK (status IN ('succeeded', 'failed', 'superseded')),
  row_count       bigint,
  checks          jsonb NOT NULL DEFAULT '[]',
  error_code      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, billing_period, export_version)
);
CREATE INDEX ON ingest_batch (tenant_id, created_at DESC);

CREATE TABLE billing_file (
  id             uuid PRIMARY KEY,
  tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id  uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  batch_id       uuid NOT NULL REFERENCES ingest_batch(id) ON DELETE CASCADE,
  billing_period date NOT NULL,
  export_version text NOT NULL,
  object_key     text NOT NULL,
  sha256         char(64) NOT NULL,
  size_bytes     bigint NOT NULL,
  row_count      bigint,
  status         text NOT NULL
                 CHECK (status IN ('landed', 'aggregated', 'superseded', 'failed')),
  UNIQUE (tenant_id, connection_id, billing_period, export_version, object_key)
);

CREATE TABLE tag_set (
  tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  hash      bytea NOT NULL,
  tags      jsonb NOT NULL,
  PRIMARY KEY (tenant_id, hash)
);

CREATE TABLE cost_daily (
  tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id      uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  usage_date         date NOT NULL,
  billing_period     date NOT NULL,
  account_id         char(12) NOT NULL,
  service_code       text NOT NULL,
  service_name       text NOT NULL,
  service_category   text NOT NULL,
  region_id          text,
  usage_type         text,
  charge_category    text NOT NULL
                     CHECK (charge_category IN ('Usage', 'Purchase', 'Tax', 'Credit',
                                                'Adjustment')),
  tag_set_hash       bytea,
  billed_cost_usd    numeric(18, 6) NOT NULL,
  effective_cost_usd numeric(18, 6) NOT NULL,
  net_cost_usd       numeric(18, 6) NOT NULL,
  list_cost_usd      numeric(18, 6),
  usage_quantity     numeric(24, 6),
  data_version       text NOT NULL
);
CREATE INDEX ON cost_daily (tenant_id, usage_date);
CREATE INDEX ON cost_daily (tenant_id, service_code, usage_date);
CREATE INDEX ON cost_daily (tenant_id, account_id, usage_date);
CREATE INDEX ON cost_daily (connection_id, billing_period);

-- The CUR poll runs without a tenant; it may read connections (never write) when the
-- job marks its transaction with scheduler_scope().
CREATE FUNCTION app_scheduler() RETURNS boolean LANGUAGE sql STABLE AS
  $$ SELECT coalesce(current_setting('app.scheduler', true), '') = 'on' $$;
GRANT EXECUTE ON FUNCTION app_scheduler() TO {APP_ROLE};
CREATE POLICY scheduler_read ON cloud_connection FOR SELECT USING (app_scheduler());
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
DROP POLICY IF EXISTS scheduler_read ON cloud_connection;
DROP FUNCTION IF EXISTS app_scheduler();
DROP TABLE IF EXISTS cost_daily, tag_set, billing_file, ingest_batch CASCADE;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
