"""Tenancy, identity, cloud connections and audit log, with row-level security.

Revision ID: 0001
Revises:
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"
TENANT_TABLES = ("cloud_connection", "cloud_account", "sync_run")

UPGRADE = f"""
CREATE EXTENSION IF NOT EXISTS citext;

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    CREATE ROLE {APP_ROLE} NOLOGIN NOBYPASSRLS;
  END IF;
END $$;

-- Request context set by cloudoptics.platform.db.scope(); NULL when unset or empty.
CREATE FUNCTION app_current_user() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_current_tenant() RETURNS uuid LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;

CREATE TABLE tenant (
  id                 uuid PRIMARY KEY,
  parent_tenant_id   uuid REFERENCES tenant(id),
  name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  country_code       char(2) NOT NULL,
  display_currency   char(3) NOT NULL,
  fx_rate_basis      text NOT NULL DEFAULT 'official',
  time_zone          text NOT NULL DEFAULT 'Africa/Lagos',
  fiscal_month_start smallint NOT NULL DEFAULT 1 CHECK (fiscal_month_start BETWEEN 1 AND 12),
  mfa_required       boolean NOT NULL DEFAULT false,
  status             text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'suspended', 'deleting')),
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
  id                      uuid PRIMARY KEY,
  email                   citext UNIQUE NOT NULL,
  display_name            text,
  auth_subject            text UNIQUE NOT NULL,
  mfa_enabled             boolean NOT NULL DEFAULT false,
  preferred_currency_view text NOT NULL DEFAULT 'both'
                          CHECK (preferred_currency_view IN ('usd', 'local', 'both')),
  created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE membership (
  tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX ON membership (user_id);

CREATE TABLE cloud_connection (
  id                uuid PRIMARY KEY,
  tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  provider          text NOT NULL DEFAULT 'aws',
  aws_account_id    char(12) NOT NULL CHECK (aws_account_id ~ '^[0-9]{{12}}$'),
  account_type      text NOT NULL CHECK (account_type IN ('management', 'standalone')),
  role_arn          text NOT NULL,
  external_id_enc   bytea NOT NULL,
  stack_name        text NOT NULL,
  create_cur_export boolean NOT NULL DEFAULT true,
  cur_bucket        text, cur_prefix text, cur_export_arn text, cur_region text,
  status            text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'validating', 'healthy', 'degraded',
                                      'failed', 'disconnected')),
  permission_checks jsonb NOT NULL DEFAULT '[]',
  last_error        jsonb,
  last_validated_at timestamptz,
  created_by        uuid REFERENCES app_user(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, aws_account_id)
);
CREATE INDEX ON cloud_connection (tenant_id);

CREATE TABLE cloud_account (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id   uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  aws_account_id  char(12) NOT NULL,
  name            text,
  account_type    text NOT NULL CHECK (account_type IN ('management', 'member', 'standalone')),
  resource_access boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, aws_account_id)
);

CREATE TABLE sync_run (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  connection_id uuid NOT NULL REFERENCES cloud_connection(id) ON DELETE CASCADE,
  job_type      text NOT NULL,
  scope         jsonb,
  status        text NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
  started_at    timestamptz NOT NULL,
  finished_at   timestamptz,
  records       bigint,
  error_code    text,
  error_detail  text,
  est_customer_api_cost_usd numeric(10, 4) DEFAULT 0
);
CREATE INDEX ON sync_run (tenant_id, connection_id, started_at DESC);

CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  tenant_id   uuid,
  actor_type  text NOT NULL CHECK (actor_type IN ('user', 'system', 'staff')),
  actor_id    uuid,
  action      text NOT NULL,
  target_type text,
  target_id   text,
  before      jsonb,
  after       jsonb,
  ip          inet,
  user_agent  text,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  prev_hash   bytea,
  row_hash    bytea NOT NULL
);
CREATE INDEX ON audit_log (tenant_id, id DESC);

-- Row-level security. FORCE applies the policies to the table owner too.
ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_visible ON tenant
  USING (id = app_current_tenant()
         OR EXISTS (SELECT 1 FROM membership m
                    WHERE m.tenant_id = tenant.id AND m.user_id = app_current_user()))
  WITH CHECK (id = app_current_tenant());

ALTER TABLE membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE membership FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_visible ON membership
  USING (tenant_id = app_current_tenant() OR user_id = app_current_user())
  WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_read ON audit_log FOR SELECT
  USING (tenant_id = app_current_tenant());
CREATE POLICY audit_append ON audit_log FOR INSERT
  WITH CHECK (tenant_id IS NULL OR tenant_id = app_current_tenant());

GRANT USAGE ON SCHEMA public TO {APP_ROLE};
GRANT EXECUTE ON FUNCTION app_current_user(), app_current_tenant() TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant, membership TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE ON app_user TO {APP_ROLE};
GRANT SELECT, INSERT ON audit_log TO {APP_ROLE};
GRANT USAGE ON SEQUENCE audit_log_id_seq TO {APP_ROLE};
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
DROP TABLE IF EXISTS audit_log, sync_run, cloud_account, cloud_connection, membership,
  app_user, tenant CASCADE;
DROP FUNCTION IF EXISTS app_current_user(), app_current_tenant();
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
