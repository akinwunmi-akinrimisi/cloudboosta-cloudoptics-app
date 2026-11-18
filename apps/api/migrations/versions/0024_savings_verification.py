"""Recommendation verification and realised savings (WBS AN-16, PRD US-5.1/5.2, FR-REC-07).

- ``recommendation``: the lifecycle gains ``not_verified`` (marked done, but 72 hours on
  the latest scan still shows the change not made). New columns:
  - ``done_at``: when a member marked it done.
  - ``verified_at``: when a scan confirmed the change (or the resource went away).
  - ``changed_at``: when the change happened, as near as the scans and the done date tell.
    Realised savings compare the bills before and after this day.
  - ``baseline``: the configuration and estimate the change is checked against, kept when
    it is marked done (a later run can update the recommendation's own columns).
  - ``verification``: the last check (``check``, ``outcome``, ``estimateUsdMonth``) and
    the last measurement of the bills (``measurement``).
  - ``realised_savings_usd_month`` / ``realised_basis`` / ``realised_at``: the saving,
    normalised to 30 days, from the resource's own costs in the CUR (``cur``), or the
    estimate when the bills do not show the resource (``estimate``).
- ``recommendation_event.kind`` gains ``savings_measured``. Checks by Cloudoptics are
  ``status_changed`` events without an actor.
- ``cost_resource_daily`` (migration 0023): ingestion now keeps every usage line with a
  resource ID, not only the R6b/R10 ones; an index serves the per-resource before and
  after comparisons.

Existing rows: ``done`` and ``verified`` get ``done_at`` from their history (or the last
run), ``verified`` and ``resolved_externally`` get ``changed_at`` and ``verified_at`` from
the last run, and all three get a baseline from their current columns. Their realised
savings are then measured by the next verification run.

Revision ID: 0024
Revises: 0023
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

STATUSES = (
    "'new', 'assigned', 'in_progress', 'done', 'verified', 'not_verified', 'dismissed', "
    "'snoozed', 'resolved_externally', 'expired'"
)
OLD_STATUSES = (
    "'new', 'assigned', 'in_progress', 'done', 'verified', 'dismissed', 'snoozed', "
    "'resolved_externally', 'expired'"
)

UPGRADE = f"""
ALTER TABLE recommendation DROP CONSTRAINT recommendation_status_check;
ALTER TABLE recommendation ADD CONSTRAINT recommendation_status_check
  CHECK (status IN ({STATUSES}));
ALTER TABLE recommendation
  ADD COLUMN done_at timestamptz,
  ADD COLUMN verified_at timestamptz,
  ADD COLUMN changed_at timestamptz,
  ADD COLUMN baseline jsonb,
  ADD COLUMN verification jsonb,
  ADD COLUMN realised_savings_usd_month numeric(18, 6),
  ADD COLUMN realised_basis text CHECK (realised_basis IN ('cur', 'estimate')),
  ADD COLUMN realised_at timestamptz;
CREATE INDEX ON recommendation (tenant_id, status)
  WHERE status IN ('done', 'not_verified', 'verified', 'resolved_externally');

ALTER TABLE recommendation_event DROP CONSTRAINT recommendation_event_kind_check;
ALTER TABLE recommendation_event ADD CONSTRAINT recommendation_event_kind_check
  CHECK (kind IN ('status_changed', 'assigned', 'commented', 'savings_measured'));

-- The schema owner is subject to the forced policies: lift them for the backfill only.
ALTER TABLE recommendation NO FORCE ROW LEVEL SECURITY;
ALTER TABLE recommendation_event NO FORCE ROW LEVEL SECURITY;
UPDATE recommendation r SET done_at = coalesce(
  (SELECT max(e.created_at) FROM recommendation_event e WHERE e.recommendation_id = r.id
   AND e.kind = 'status_changed' AND e.to_status = 'done'), r.last_evaluated_at)
  WHERE r.status IN ('done', 'verified');
UPDATE recommendation SET verified_at = last_evaluated_at, changed_at = last_evaluated_at
  WHERE status = 'verified';
UPDATE recommendation SET changed_at = last_evaluated_at WHERE status = 'resolved_externally';
UPDATE recommendation SET baseline = jsonb_build_object(
  'currentConfig', current_config, 'recommendedConfig', recommended_config,
  'savingsUsdMonth', savings_usd_month)
  WHERE status IN ('done', 'verified', 'resolved_externally');
ALTER TABLE recommendation FORCE ROW LEVEL SECURITY;
ALTER TABLE recommendation_event FORCE ROW LEVEL SECURITY;

CREATE INDEX cost_resource_daily_resource_idx
  ON cost_resource_daily (tenant_id, account_id, resource_id, usage_date);
"""

DOWNGRADE = f"""
DROP INDEX IF EXISTS cost_resource_daily_resource_idx;
ALTER TABLE recommendation NO FORCE ROW LEVEL SECURITY;
ALTER TABLE recommendation_event NO FORCE ROW LEVEL SECURITY;
DELETE FROM recommendation_event WHERE kind = 'savings_measured';
UPDATE recommendation SET status = 'done' WHERE status = 'not_verified';
ALTER TABLE recommendation FORCE ROW LEVEL SECURITY;
ALTER TABLE recommendation_event FORCE ROW LEVEL SECURITY;
ALTER TABLE recommendation_event DROP CONSTRAINT recommendation_event_kind_check;
ALTER TABLE recommendation_event ADD CONSTRAINT recommendation_event_kind_check
  CHECK (kind IN ('status_changed', 'assigned', 'commented'));
ALTER TABLE recommendation DROP CONSTRAINT recommendation_status_check;
ALTER TABLE recommendation ADD CONSTRAINT recommendation_status_check
  CHECK (status IN ({OLD_STATUSES}));
ALTER TABLE recommendation
  DROP COLUMN IF EXISTS done_at,
  DROP COLUMN IF EXISTS verified_at,
  DROP COLUMN IF EXISTS changed_at,
  DROP COLUMN IF EXISTS baseline,
  DROP COLUMN IF EXISTS verification,
  DROP COLUMN IF EXISTS realised_savings_usd_month,
  DROP COLUMN IF EXISTS realised_basis,
  DROP COLUMN IF EXISTS realised_at;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
