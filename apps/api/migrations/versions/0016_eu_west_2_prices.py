"""EU (London) list prices for the inventory rules (PRD FR-REC-02).

Until now only us-east-1 prices (region ``*``) were seeded, so savings in eu-west-2 used
prices about 16% lower than London's and gp2 was priced as if it cost the same as in
us-east-1. On-demand prices from the AWS Price List (AmazonEC2 and AWSELB offer files for
eu-west-2, October 2026). Public IPv4 costs the same everywhere and keeps its ``*`` row.

Revision ID: 0016
Revises: 0015
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

EU_WEST_2 = {
    "ebs_gp2_gb_month": 0.116,
    "ebs_gp3_gb_month": 0.0928,
    "ebs_gp3_iops_month": 0.0058,
    "ebs_gp3_throughput_month": 0.0464,
    "ebs_io1_gb_month": 0.145,
    "ebs_io1_iops_month": 0.076,
    "ebs_io2_gb_month": 0.145,
    "ebs_st1_gb_month": 0.053,
    "ebs_sc1_gb_month": 0.0174,
    "ebs_standard_gb_month": 0.058,
    "ebs_snapshot_gb_month": 0.053,
    "ebs_snapshot_archive_gb_month": 0.01325,
    "alb_hour": 0.02646,
    "nlb_hour": 0.02646,
    "gwlb_hour": 0.0147,
    "clb_hour": 0.0294,
}

_rows = ",\n".join(
    f"  ('eu-west-2', '{item}', {price}, 'seed')" for item, price in EU_WEST_2.items()
)

UPGRADE = f"""
INSERT INTO price_catalog (region_id, item, price_usd, source) VALUES
{_rows}
ON CONFLICT (region_id, item) DO NOTHING;
"""

DOWNGRADE = """
DELETE FROM price_catalog WHERE region_id = 'eu-west-2' AND source = 'seed';
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
