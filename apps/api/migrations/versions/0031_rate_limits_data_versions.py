"""Rate-limit counters and per-tenant data versions (WBS API-8).

- ``rate_limit_counter``: fixed one-minute windows shared by every API instance. Keys are
  ``user:<id>``, ``tenant:<id>`` or ``ip:<route>:<sha256 of the address>``; rows older than a
  few minutes are deleted by the API as it goes. No tenant data, so no RLS; the app role
  reads and writes it.
- ``tenant_data_version``: a counter per tenant, bumped once by every committed transaction
  that changed one of the tenant's data tables (``VERSIONED_TABLES``). ETags and the query
  cache are keyed by it. Triggers maintain it, so no write path can forget: a statement-level
  trigger notes the tenants a statement touched in ``data_version_pending`` (keyed by the
  transaction, so concurrent writers never wait on each other), and a deferred constraint
  trigger on that table bumps the counter at commit, so the counter row is locked only for
  the moment of the commit and never for a long ingestion.
- ``fx_data_version``: one counter for the shared exchange-rate table (forecasts and
  commitments convert with it).

The trigger functions are SECURITY DEFINER and owned by the migration role. The version
tables have RLS enabled but not forced, so the owner (the trigger) writes them whatever the
writer's tenant context (migrations, jobs), while the app role can only read its own
tenant's row and cannot write a version at all.

Tables left out on purpose (no ETagged response reads them, or they change on nearly
every request): audit_log, membership, invite, connection_intent, notification,
notification_preference, resource_metric, aws_api_usage.

Revision ID: 0031
Revises: 0028
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0031"
down_revision = "0028"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

VERSIONED_TABLES = (
    "cloud_connection",
    "cloud_account",
    "sync_run",
    "ingest_batch",
    "billing_file",
    "tag_set",
    "cost_daily",
    "cost_snapshot_ce",
    "cost_reconciliation",
    "cost_resource_daily",
    "resource",
    "recommendation",
    "recommendation_event",
    "rule_setting",
    "budget",
    "anomaly",
    "alert_setting",
    "monthly_report",
    "report_setting",
    "credit_balance",
    "commitment",
    "commitment_snapshot",
)

UPGRADE = f"""
CREATE TABLE rate_limit_counter (
  bucket       text NOT NULL,
  window_start timestamptz NOT NULL,
  hits         integer NOT NULL,
  PRIMARY KEY (bucket, window_start)
);
CREATE INDEX ON rate_limit_counter (window_start);
GRANT SELECT, INSERT, UPDATE, DELETE ON rate_limit_counter TO {APP_ROLE};

CREATE TABLE tenant_data_version (
  tenant_id  uuid PRIMARY KEY,
  version    bigint NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE tenant_data_version ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_data_version FOR SELECT
  USING (tenant_id = app_current_tenant());
GRANT SELECT ON tenant_data_version TO {APP_ROLE};

CREATE TABLE data_version_pending (
  tenant_id uuid NOT NULL,
  xact      xid8 NOT NULL,
  PRIMARY KEY (tenant_id, xact)
);

CREATE TABLE fx_data_version (
  id      boolean PRIMARY KEY DEFAULT true CHECK (id),
  version bigint NOT NULL
);
INSERT INTO fx_data_version (id, version) VALUES (true, 1);
GRANT SELECT ON fx_data_version TO {APP_ROLE};

-- Statement level: note each tenant the statement touched, once per transaction.
CREATE FUNCTION data_version_note() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  INSERT INTO data_version_pending (tenant_id, xact)
  SELECT DISTINCT tenant_id, pg_current_xact_id() FROM changed WHERE tenant_id IS NOT NULL
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;

-- The tenant table keys its rows by id.
CREATE FUNCTION data_version_note_tenant() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  INSERT INTO data_version_pending (tenant_id, xact)
  SELECT DISTINCT id, pg_current_xact_id() FROM changed
  ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;

-- Deferred to commit: bump the tenant's counter once for the transaction.
CREATE FUNCTION data_version_bump() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  INSERT INTO tenant_data_version AS v (tenant_id, version, changed_at)
  VALUES (NEW.tenant_id, 1, now())
  ON CONFLICT (tenant_id) DO UPDATE SET version = v.version + 1, changed_at = now();
  DELETE FROM data_version_pending WHERE tenant_id = NEW.tenant_id AND xact = NEW.xact;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER data_version_bump AFTER INSERT ON data_version_pending
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION data_version_bump();

CREATE FUNCTION fx_data_version_bump() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE fx_data_version SET version = version + 1;
  RETURN NULL;
END $$;

CREATE TRIGGER fx_data_version AFTER INSERT OR UPDATE OR DELETE ON fx_rate
  FOR EACH STATEMENT EXECUTE FUNCTION fx_data_version_bump();

REVOKE ALL ON FUNCTION data_version_note(), data_version_note_tenant(), data_version_bump(),
  fx_data_version_bump() FROM PUBLIC;

CREATE TRIGGER data_version_ins AFTER INSERT ON tenant
  REFERENCING NEW TABLE AS changed FOR EACH STATEMENT
  EXECUTE FUNCTION data_version_note_tenant();
CREATE TRIGGER data_version_upd AFTER UPDATE ON tenant
  REFERENCING NEW TABLE AS changed FOR EACH STATEMENT
  EXECUTE FUNCTION data_version_note_tenant();
"""

for _table in VERSIONED_TABLES:
    UPGRADE += f"""
CREATE TRIGGER data_version_ins AFTER INSERT ON {_table}
  REFERENCING NEW TABLE AS changed FOR EACH STATEMENT EXECUTE FUNCTION data_version_note();
CREATE TRIGGER data_version_upd AFTER UPDATE ON {_table}
  REFERENCING NEW TABLE AS changed FOR EACH STATEMENT EXECUTE FUNCTION data_version_note();
CREATE TRIGGER data_version_del AFTER DELETE ON {_table}
  REFERENCING OLD TABLE AS changed FOR EACH STATEMENT EXECUTE FUNCTION data_version_note();
"""

DOWNGRADE = "DROP TRIGGER IF EXISTS fx_data_version ON fx_rate;\n"
for _table in (*VERSIONED_TABLES, "tenant"):
    for _event in ("ins", "upd", "del"):
        DOWNGRADE += f"DROP TRIGGER IF EXISTS data_version_{_event} ON {_table};\n"
DOWNGRADE += """
DROP TABLE IF EXISTS data_version_pending, tenant_data_version, fx_data_version,
  rate_limit_counter CASCADE;
DROP FUNCTION IF EXISTS data_version_note(), data_version_note_tenant(), data_version_bump(),
  fx_data_version_bump();
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
