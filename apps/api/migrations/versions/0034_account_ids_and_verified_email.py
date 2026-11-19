"""Account IDs and verified emails (security audit run 1, leads 14 and 17).

- ``cloud_account.aws_account_id`` must be 12 digits, as ``cloud_connection``'s already is.
  Account IDs come from the customer's bills too; one that is not 12 digits is not an AWS
  account, so rows holding one are removed first (nothing references cloud_account rows).
- ``app_user.email_verified``: whether the sign-in provider confirmed the email the row
  holds. Sign-in uses it to decide which of two identities presenting one email keeps it
  (identity/deps.py). Existing rows start as not verified and are updated at their next
  sign-in.

The app role already has SELECT, INSERT and UPDATE on app_user (0001), which cover the new
column.

Revision ID: 0034
Revises: 0033
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE cloud_account NO FORCE ROW LEVEL SECURITY;
DELETE FROM cloud_account WHERE aws_account_id !~ '^[0-9]{12}$';
ALTER TABLE cloud_account FORCE ROW LEVEL SECURITY;
ALTER TABLE cloud_account ADD CONSTRAINT cloud_account_aws_account_id_check
  CHECK (aws_account_id ~ '^[0-9]{12}$');

ALTER TABLE app_user ADD COLUMN email_verified boolean NOT NULL DEFAULT false;
"""

DOWNGRADE = """
ALTER TABLE app_user DROP COLUMN IF EXISTS email_verified;
ALTER TABLE cloud_account DROP CONSTRAINT IF EXISTS cloud_account_aws_account_id_check;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
