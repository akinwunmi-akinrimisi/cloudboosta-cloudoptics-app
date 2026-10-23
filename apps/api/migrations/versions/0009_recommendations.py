"""Recommendation engine: list prices, recommendations and when a resource's state began.

- ``resource.state_since``: when the scan first saw the current state (detach time for
  volumes, idle time for IPs and load balancers; PRD FR-ANA-01/02).
- ``price_catalog`` (global): on-demand list prices per region, used when the tenant's own
  effective rate is not in its CUR (FR-REC-02). Region ``*`` holds the us-east-1 list
  prices used as the fallback for any region without its own row; those estimates are
  labelled as such.
- ``recommendation`` (docs/09 §2.2), under tenant row-level security.

Revision ID: 0009
Revises: 0008
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

# us-east-1 on-demand list prices (USD), October 2026. Public IPv4 is priced the same in
# every region.
SEED_PRICES = {
    "ebs_gp2_gb_month": 0.10,
    "ebs_gp3_gb_month": 0.08,
    "ebs_gp3_iops_month": 0.005,
    "ebs_gp3_throughput_month": 0.04,
    "ebs_io1_gb_month": 0.125,
    "ebs_io1_iops_month": 0.065,
    "ebs_io2_gb_month": 0.125,
    "ebs_st1_gb_month": 0.045,
    "ebs_sc1_gb_month": 0.015,
    "ebs_standard_gb_month": 0.05,
    "ebs_snapshot_gb_month": 0.05,
    "ebs_snapshot_archive_gb_month": 0.0125,
    "public_ipv4_hour": 0.005,
    "alb_hour": 0.0225,
    "nlb_hour": 0.0225,
    "gwlb_hour": 0.0125,
    "clb_hour": 0.025,
}

_seed = ",\n".join(f"  ('*', '{item}', {price}, 'seed')" for item, price in SEED_PRICES.items())

UPGRADE = f"""
ALTER TABLE resource ADD COLUMN state_since timestamptz NOT NULL DEFAULT now();

CREATE TABLE price_catalog (
  region_id  text NOT NULL,
  item       text NOT NULL,
  price_usd  numeric(18, 8) NOT NULL CHECK (price_usd >= 0),
  source     text NOT NULL CHECK (source IN ('seed', 'price_list')),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (region_id, item)
);
INSERT INTO price_catalog (region_id, item, price_usd, source) VALUES
{_seed};
GRANT SELECT ON price_catalog TO {APP_ROLE};

CREATE TABLE recommendation (
  id                   uuid PRIMARY KEY,
  tenant_id            uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  fingerprint          text NOT NULL,
  type                 text NOT NULL,
  rule_id              text NOT NULL,
  rule_version         integer NOT NULL,
  rule_config          jsonb NOT NULL,
  generation_id        bigint NOT NULL,
  resource_ref         uuid REFERENCES resource(id) ON DELETE SET NULL,
  account_id           char(12) NOT NULL,
  region_id            text NOT NULL,
  resource_id          text NOT NULL,
  title                text NOT NULL,
  current_config       jsonb NOT NULL DEFAULT '{{}}',
  recommended_config   jsonb NOT NULL DEFAULT '{{}}',
  cost_now_usd_month   numeric(18, 6) NOT NULL,
  cost_after_usd_month numeric(18, 6) NOT NULL,
  savings_usd_month    numeric(18, 6)
                       GENERATED ALWAYS AS (cost_now_usd_month - cost_after_usd_month) STORED,
  savings_basis        text NOT NULL CHECK (savings_basis IN ('effective_rate', 'list_price')),
  effort               text NOT NULL CHECK (effort IN ('S', 'M', 'L')),
  reversibility        text NOT NULL
                       CHECK (reversibility IN ('reversible', 'with_snapshot', 'irreversible')),
  restart_required     boolean NOT NULL DEFAULT false,
  risk_score           smallint NOT NULL,
  risk_level           text NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
  risk_factors         jsonb NOT NULL DEFAULT '[]',
  confidence           text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
  confidence_notes     jsonb NOT NULL DEFAULT '[]',
  priority_score       numeric(18, 6) NOT NULL,
  evidence             jsonb NOT NULL DEFAULT '[]',
  fix                  jsonb NOT NULL DEFAULT '{{}}',
  primary_of           uuid REFERENCES recommendation(id) ON DELETE SET NULL,
  status               text NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new', 'assigned', 'in_progress', 'done', 'verified',
                                         'dismissed', 'snoozed', 'resolved_externally',
                                         'expired')),
  engine_version       text NOT NULL,
  first_detected_at    timestamptz NOT NULL DEFAULT now(),
  last_evaluated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, fingerprint)
);
CREATE INDEX ON recommendation (tenant_id, status, priority_score DESC);

ALTER TABLE recommendation ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recommendation
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON recommendation TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS recommendation CASCADE;
DROP TABLE IF EXISTS price_catalog CASCADE;
ALTER TABLE resource DROP COLUMN IF EXISTS state_since;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
