"""Exchange rates (WBS IN-8, ADR-010, PRD FR-FX-01..04).

A global, append-only table: staff enter rates by hand (``source = 'manual'``) and a daily
feed adds reference rates (``source = 'feed'``). A correction is a new row; for a given
currency, basis, source and date the newest row wins. The app role may read and append,
never edit or delete, so the history stays auditable.

Revision ID: 0005
Revises: 0004
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE fx_rate (
  id          uuid PRIMARY KEY,
  currency    char(3) NOT NULL CHECK (currency ~ '^[A-Z]{{3}}$'),
  rate_date   date NOT NULL,
  basis       text NOT NULL CHECK (basis IN ('official', 'market')),
  rate        numeric(18, 8) NOT NULL CHECK (rate > 0),
  source      text NOT NULL CHECK (source IN ('manual', 'feed')),
  source_note text,
  reason      text,
  entered_by  uuid REFERENCES app_user(id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (source = 'feed' OR entered_by IS NOT NULL)
);
CREATE INDEX ON fx_rate (currency, source, basis, rate_date DESC, created_at DESC);

GRANT SELECT, INSERT ON fx_rate TO {APP_ROLE};
"""

DOWNGRADE = "DROP TABLE IF EXISTS fx_rate CASCADE;"


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
