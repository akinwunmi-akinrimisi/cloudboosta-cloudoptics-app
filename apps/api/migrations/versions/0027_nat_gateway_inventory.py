"""NAT gateways in the inventory (R10 route tables, access template 2.0.4).

- ``resource.resource_type`` also allows ``nat_gateway``. The scan stores each gateway with
  the route tables that send traffic to it in ``config`` (``routeTables``,
  ``routeTablesStatus``, ``routeTablesCheckedAt``), so no new table is needed.

Revision ID: 0027
Revises: 0026
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE resource DROP CONSTRAINT resource_resource_type_check;
ALTER TABLE resource ADD CONSTRAINT resource_resource_type_check CHECK (resource_type IN
  ('ec2_instance', 'ebs_volume', 'ebs_snapshot', 'eip', 'elb', 'rds_instance', 'nat_gateway'));
"""

# Recommendations on a removed gateway keep their row (resource_ref is set to NULL).
DOWNGRADE = """
ALTER TABLE resource NO FORCE ROW LEVEL SECURITY;
DELETE FROM resource WHERE resource_type = 'nat_gateway';
ALTER TABLE resource FORCE ROW LEVEL SECURITY;
ALTER TABLE resource DROP CONSTRAINT resource_resource_type_check;
ALTER TABLE resource ADD CONSTRAINT resource_resource_type_check CHECK (resource_type IN
  ('ec2_instance', 'ebs_volume', 'ebs_snapshot', 'eip', 'elb', 'rds_instance'));
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
