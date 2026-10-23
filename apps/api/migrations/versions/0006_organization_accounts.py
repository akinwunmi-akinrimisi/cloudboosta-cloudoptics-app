"""AWS Organizations account discovery (WBS CC-7, PRD US-1.3).

Adds the account's status in the organization (``ACTIVE``, ``SUSPENDED``,
``PENDING_CLOSURE``, or ``NOT_LISTED`` once it no longer appears) and when discovery last
saw it. Both stay NULL for accounts that only come from a connection or the CUR.

Revision ID: 0006
Revises: 0005
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE cloud_account ADD COLUMN org_status text;
ALTER TABLE cloud_account ADD COLUMN org_seen_at timestamptz;
"""

DOWNGRADE = """
ALTER TABLE cloud_account DROP COLUMN IF EXISTS org_seen_at;
ALTER TABLE cloud_account DROP COLUMN IF EXISTS org_status;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
