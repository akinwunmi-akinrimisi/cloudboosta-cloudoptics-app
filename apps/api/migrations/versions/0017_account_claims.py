"""An AWS account is reserved for a workspace only once it proves control of it (security audit,
27 Oct 2026).

The global ``UNIQUE (provider, aws_account_id)`` let any workspace claim any 12-digit account
ID with no proof, so the real owner could never connect it, and the error told the claimant
which accounts were already customers. Now:

- ``account_verified_at``: when a validation first assumed the role in that account (only
  possible with the stack deployed there, using this connection's External ID).
- One verified, connected workspace per account (partial unique index); unverified claims
  block nobody. Inside a workspace an account still has one connection.

Revision ID: 0017
Revises: 0016
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER TABLE cloud_connection ADD COLUMN account_verified_at timestamptz;
UPDATE cloud_connection SET account_verified_at = coalesce(last_validated_at, created_at)
  WHERE status IN ('healthy', 'degraded');
ALTER TABLE cloud_connection DROP CONSTRAINT cloud_connection_provider_aws_account_id_key;
CREATE UNIQUE INDEX cloud_connection_tenant_account_key
  ON cloud_connection (tenant_id, provider, aws_account_id);
CREATE UNIQUE INDEX cloud_connection_verified_account_key
  ON cloud_connection (provider, aws_account_id)
  WHERE account_verified_at IS NOT NULL AND status <> 'disconnected';
"""

DOWNGRADE = """
DROP INDEX cloud_connection_verified_account_key;
DROP INDEX cloud_connection_tenant_account_key;
ALTER TABLE cloud_connection ADD CONSTRAINT cloud_connection_provider_aws_account_id_key
  UNIQUE (provider, aws_account_id);
ALTER TABLE cloud_connection DROP COLUMN account_verified_at;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
