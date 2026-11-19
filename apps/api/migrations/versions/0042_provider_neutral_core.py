"""Provider-neutral core, part 1: the provider on every fact table and neutral account IDs
(ADR-018, multi-cloud plan Phase 1). Nothing changes for AWS data.

- ``provider`` (``aws``, ``gcp``, ``azure`` or ``digitalocean``; default ``aws``) is now
  CHECKed on ``cloud_connection`` (where it existed, unread, since 0001) and added to
  ``cloud_account``, ``cost_daily``, ``cost_resource_daily``, ``cost_snapshot_ce``,
  ``resource``, ``recommendation``, ``anomaly``, ``commitment`` and ``price_catalog``. Adding
  a column with a constant default rewrites nothing, so existing rows are ``aws`` at once.
- The 12-digit AWS columns become ``provider_account_id text``: ``cloud_connection`` and
  ``cloud_account`` (``aws_account_id``), and the fact tables (``account_id``). The 12-digit
  CHECK now applies only where ``provider = 'aws'``; every provider's ID must be 1-128
  characters. On the fact tables, which had no CHECK (``char(12)`` only), the AWS check is
  added NOT VALID: new rows are checked, existing rows came from checked writers.
  ``char(12)`` to ``text`` rewrites those tables once (a lock for the length of the copy).
- Keys that two providers' accounts could collide on now include the provider:
  ``cloud_account`` (tenant, provider, account), ``resource`` (tenant, provider, account,
  region, resource ID), ``anomaly`` (tenant, day, provider, account, service) and the
  ``price_catalog`` primary key (provider, region, item). ``cloud_connection``'s two account
  keys (one per workspace, one verified workspace per account, ADR-017) already had the
  provider and keep it.
- ``resource.resource_type`` keeps its names (``ec2_instance``...); the CHECK on them binds
  AWS rows only, and the provider is part of the key instead (ADR-018 explains why names are
  not namespaced).

Tables keep FORCE RLS throughout: no data is changed here. The downgrade refuses to run while
any non-AWS row exists (``char(12)`` cannot hold it); with AWS data only it is lossless.

Not online-compatible with the previous code (renamed columns): deploy with the worker
stopped while the migration runs (ADR-018, "Deploying").

Revision ID: 0042
Revises: 0041
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

PROVIDERS = "('aws', 'gcp', 'azure', 'digitalocean')"
AWS_ID = "provider_account_id ~ '^[0-9]{12}$'"
ID_LENGTH = "length(provider_account_id) BETWEEN 1 AND 128"

# Tables that gain the provider column (cloud_connection has it already).
NEW_PROVIDER = (
    "cloud_account",
    "cost_daily",
    "cost_resource_daily",
    "cost_snapshot_ce",
    "resource",
    "recommendation",
    "anomaly",
    "commitment",
    "price_catalog",
)
# Fact tables whose char(12) account_id becomes provider_account_id text.
FACT_ACCOUNT = (
    "cost_daily",
    "cost_resource_daily",
    "resource",
    "recommendation",
    "anomaly",
    "commitment",
)
AWS_TYPES = (
    "('ec2_instance', 'ebs_volume', 'ebs_snapshot', 'eip', 'elb', 'rds_instance', 'nat_gateway')"
)

UPGRADE = f"""
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_provider_check
  CHECK (provider IN {PROVIDERS});
"""
for _t in NEW_PROVIDER:
    UPGRADE += (
        f"ALTER TABLE {_t} ADD COLUMN provider text NOT NULL DEFAULT 'aws'\n"
        f"  CONSTRAINT {_t}_provider_check CHECK (provider IN {PROVIDERS});\n"
    )

UPGRADE += f"""
ALTER TABLE cloud_connection RENAME COLUMN aws_account_id TO provider_account_id;
ALTER TABLE cloud_connection ALTER COLUMN provider_account_id TYPE text;
ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_aws_account_id_check;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_provider_account_id_check
  CHECK ({ID_LENGTH} AND (provider <> 'aws' OR {AWS_ID}));

ALTER TABLE cloud_account RENAME COLUMN aws_account_id TO provider_account_id;
ALTER TABLE cloud_account ALTER COLUMN provider_account_id TYPE text;
ALTER TABLE cloud_account DROP CONSTRAINT cloud_account_aws_account_id_check;
ALTER TABLE cloud_account ADD CONSTRAINT cloud_account_provider_account_id_check
  CHECK ({ID_LENGTH} AND (provider <> 'aws' OR {AWS_ID}));
ALTER TABLE cloud_account DROP CONSTRAINT cloud_account_tenant_id_aws_account_id_key;
ALTER TABLE cloud_account ADD CONSTRAINT cloud_account_tenant_provider_account_key
  UNIQUE (tenant_id, provider, provider_account_id);
"""
for _t in FACT_ACCOUNT:
    UPGRADE += (
        f"ALTER TABLE {_t} RENAME COLUMN account_id TO provider_account_id;\n"
        f"ALTER TABLE {_t} ALTER COLUMN provider_account_id TYPE text;\n"
        f"ALTER TABLE {_t} ADD CONSTRAINT {_t}_provider_account_id_check\n"
        f"  CHECK ({ID_LENGTH} AND (provider <> 'aws' OR {AWS_ID})) NOT VALID;\n"
    )

UPGRADE += f"""
ALTER TABLE resource DROP CONSTRAINT resource_tenant_id_account_id_region_id_resource_id_key;
ALTER TABLE resource ADD CONSTRAINT resource_tenant_provider_account_region_resource_key
  UNIQUE (tenant_id, provider, provider_account_id, region_id, resource_id);
ALTER TABLE resource DROP CONSTRAINT resource_resource_type_check;
ALTER TABLE resource ADD CONSTRAINT resource_resource_type_check
  CHECK (provider <> 'aws' OR resource_type IN {AWS_TYPES});

ALTER TABLE anomaly DROP CONSTRAINT anomaly_tenant_id_usage_date_account_id_service_code_key;
ALTER TABLE anomaly ADD CONSTRAINT anomaly_tenant_day_provider_account_service_key
  UNIQUE (tenant_id, usage_date, provider, provider_account_id, service_code);

ALTER TABLE price_catalog DROP CONSTRAINT price_catalog_pkey;
ALTER TABLE price_catalog ADD CONSTRAINT price_catalog_pkey PRIMARY KEY (provider, region_id, item);
"""

# char(12) cannot hold another provider's ID, and AWS-only keys would merge rows: refuse.
DOWNGRADE = """
DO $$
DECLARE
  t text;
  n bigint;
BEGIN
  FOREACH t IN ARRAY ARRAY['cloud_connection', 'cloud_account', 'cost_daily',
    'cost_resource_daily', 'cost_snapshot_ce', 'resource', 'recommendation', 'anomaly',
    'commitment', 'price_catalog'] LOOP
    EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('SELECT count(*) FROM %I WHERE provider <> ''aws''', t) INTO n;
    IF t <> 'price_catalog' THEN
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END IF;
    IF n > 0 THEN
      RAISE EXCEPTION '0042 downgrade: % has % non-AWS rows; remove them first', t, n;
    END IF;
  END LOOP;
END $$;

ALTER TABLE price_catalog DROP CONSTRAINT price_catalog_pkey;
ALTER TABLE price_catalog ADD CONSTRAINT price_catalog_pkey PRIMARY KEY (region_id, item);

ALTER TABLE anomaly DROP CONSTRAINT anomaly_tenant_day_provider_account_service_key;
ALTER TABLE resource DROP CONSTRAINT resource_tenant_provider_account_region_resource_key;
ALTER TABLE resource DROP CONSTRAINT resource_resource_type_check;
ALTER TABLE resource ADD CONSTRAINT resource_resource_type_check CHECK (resource_type IN
  ('ec2_instance', 'ebs_volume', 'ebs_snapshot', 'eip', 'elb', 'rds_instance', 'nat_gateway'));
"""
for _t in FACT_ACCOUNT:
    DOWNGRADE += (
        f"ALTER TABLE {_t} DROP CONSTRAINT {_t}_provider_account_id_check;\n"
        f"ALTER TABLE {_t} ALTER COLUMN provider_account_id TYPE char(12);\n"
        f"ALTER TABLE {_t} RENAME COLUMN provider_account_id TO account_id;\n"
    )
DOWNGRADE += """
ALTER TABLE anomaly ADD CONSTRAINT anomaly_tenant_id_usage_date_account_id_service_code_key
  UNIQUE (tenant_id, usage_date, account_id, service_code);
ALTER TABLE resource ADD CONSTRAINT resource_tenant_id_account_id_region_id_resource_id_key
  UNIQUE (tenant_id, account_id, region_id, resource_id);

ALTER TABLE cloud_account DROP CONSTRAINT cloud_account_tenant_provider_account_key;
ALTER TABLE cloud_account DROP CONSTRAINT cloud_account_provider_account_id_check;
ALTER TABLE cloud_account ALTER COLUMN provider_account_id TYPE char(12);
ALTER TABLE cloud_account RENAME COLUMN provider_account_id TO aws_account_id;
ALTER TABLE cloud_account ADD CONSTRAINT cloud_account_aws_account_id_check
  CHECK (aws_account_id ~ '^[0-9]{12}$');
ALTER TABLE cloud_account ADD CONSTRAINT cloud_account_tenant_id_aws_account_id_key
  UNIQUE (tenant_id, aws_account_id);

ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_provider_account_id_check;
ALTER TABLE cloud_connection ALTER COLUMN provider_account_id TYPE char(12);
ALTER TABLE cloud_connection RENAME COLUMN provider_account_id TO aws_account_id;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_aws_account_id_check
  CHECK (aws_account_id ~ '^[0-9]{12}$');
ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_provider_check;
"""
for _t in NEW_PROVIDER:
    DOWNGRADE += f"ALTER TABLE {_t} DROP COLUMN provider;\n"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
