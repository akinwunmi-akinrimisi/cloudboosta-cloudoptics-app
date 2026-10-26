"""List prices for every AWS region (WBS IN-9, PRD FR-REC-02).

Until now only us-east-1 prices (region ``*``) were seeded, so savings everywhere else used
us-east-1 prices: London gp2, for one, was priced at $0.10 instead of $0.116. This loads
``migrations/data/price_list.json``, made by ``python -m cloudoptics.analysis.price_list``
from the AWS Price List (on-demand, AWS Price List publication of 23 Oct 2026): EBS, public
IPv4 and load balancers, and the ``large`` price of every EC2 family (Linux) and RDS family
(PostgreSQL and MySQL, Single-AZ) in each of 36 regions. The ``*`` fallback becomes a copy
of us-east-1 (two RDS MySQL seeds were slightly off).

Revision ID: 0016
Revises: 0015
"""

import json
from pathlib import Path

from cloudoptics.platform.migration_sql import run_sql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

DATA = Path(__file__).resolve().parents[1] / "data" / "price_list.json"


def _upgrade_sql() -> str:
    prices: dict[str, dict[str, float]] = json.loads(DATA.read_text())
    prices["*"] = prices["us-east-1"]
    rows = ",\n".join(
        f"  ('{region}', '{item}', {price!r}, 'price_list')"
        for region, items in sorted(prices.items())
        for item, price in sorted(items.items())
    )
    return f"""
INSERT INTO price_catalog (region_id, item, price_usd, source) VALUES
{rows}
ON CONFLICT (region_id, item) DO UPDATE SET price_usd = EXCLUDED.price_usd,
  source = EXCLUDED.source, updated_at = now();
"""


# The us-east-1 fallback rows keep their corrected prices.
DOWNGRADE = """
DELETE FROM price_catalog WHERE region_id <> '*' AND source = 'price_list';
"""


def upgrade() -> None:
    run_sql(_upgrade_sql())


def downgrade() -> None:
    run_sql(DOWNGRADE)
