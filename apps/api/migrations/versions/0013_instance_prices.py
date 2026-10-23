"""List prices for EC2 instances and RDS DB instances (PRD FR-REC-02, FR-ANA-04/06).

On-demand prices scale with size inside a family (``large`` = 1, ``xlarge`` = 2,
``2xlarge`` = 4, ``medium`` = 0.5 ...), so one row per family holds its ``large`` hourly
price and the rules scale it. Items:

- ``ec2_<family>_large_hour``: Linux, shared tenancy.
- ``rds_<engine>_<family>_large_hour``: Single-AZ, engine ``postgres`` or ``mysql``
  (MariaDB is priced as MySQL). Multi-AZ is twice the Single-AZ price.

Region ``*`` holds us-east-1 prices (October 2026), the fallback for any region without
its own row; estimates from it are labelled as such.

Revision ID: 0013
Revises: 0012
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

EC2_LARGE = {
    "t1": 0.16,
    "t2": 0.0928,
    "t3": 0.0832,
    "t3a": 0.0752,
    "t4g": 0.0672,
    "m1": 0.175,
    "m3": 0.133,
    "m4": 0.10,
    "m5": 0.096,
    "m5a": 0.086,
    "m6i": 0.096,
    "m6a": 0.0864,
    "m6g": 0.077,
    "m7i": 0.1008,
    "m7g": 0.0816,
    "c3": 0.105,
    "c4": 0.10,
    "c5": 0.085,
    "c5a": 0.077,
    "c6i": 0.085,
    "c6a": 0.0765,
    "c6g": 0.068,
    "c7i": 0.08925,
    "c7g": 0.0725,
    "r3": 0.166,
    "r4": 0.133,
    "r5": 0.126,
    "r5a": 0.113,
    "r6i": 0.126,
    "r6a": 0.1134,
    "r6g": 0.1008,
    "r7i": 0.1323,
    "r7g": 0.1071,
}

RDS_LARGE = {
    ("postgres", "t3"): 0.145,
    ("postgres", "t4g"): 0.129,
    ("postgres", "m5"): 0.178,
    ("postgres", "m6i"): 0.178,
    ("postgres", "m6g"): 0.159,
    ("postgres", "m7g"): 0.168,
    ("postgres", "r5"): 0.25,
    ("postgres", "r6i"): 0.25,
    ("postgres", "r6g"): 0.225,
    ("postgres", "r7g"): 0.239,
    ("mysql", "t3"): 0.136,
    ("mysql", "t4g"): 0.129,
    ("mysql", "m5"): 0.171,
    ("mysql", "m6i"): 0.171,
    ("mysql", "m6g"): 0.152,
    ("mysql", "m7g"): 0.161,
    ("mysql", "r5"): 0.24,
    ("mysql", "r6i"): 0.24,
    ("mysql", "r6g"): 0.215,
    ("mysql", "r7g"): 0.228,
}

ITEMS = {
    **{f"ec2_{family}_large_hour": price for family, price in EC2_LARGE.items()},
    **{f"rds_{engine}_{family}_large_hour": p for (engine, family), p in RDS_LARGE.items()},
}

_rows = ",\n".join(f"  ('*', '{item}', {price}, 'seed')" for item, price in ITEMS.items())

UPGRADE = f"""
INSERT INTO price_catalog (region_id, item, price_usd, source) VALUES
{_rows}
ON CONFLICT (region_id, item) DO NOTHING;
"""

DOWNGRADE = """
DELETE FROM price_catalog WHERE source = 'seed' AND (item LIKE 'ec2\\_%' OR item LIKE 'rds\\_%');
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
