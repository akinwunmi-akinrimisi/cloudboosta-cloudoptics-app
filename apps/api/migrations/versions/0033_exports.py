"""CSV exports (PRD US-7.2, docs/09 §3.2 "Reports & exports", WBS API-7).

- ``export_job``: one export a user asked for, and the file it produced. Small exports are
  made during the request; large ones (over 50,000 rows) by the worker, which reports
  ``progress``. The file is kept gzip-compressed in ``content`` (capped by the app at 50 MB
  compressed) until ``expires_at`` (7 days, docs/09 §4 "exports 7 days"); the app refuses to
  serve it after that, and a daily job deletes it.
- The repo has no object storage yet (raw bills are read from the customer's bucket), so the
  file lives in PostgreSQL under the same row-level security as the data it came from.
- ``scheduler_expire_*``: the daily clean-up runs without a tenant (``app_scheduler()``,
  migration 0003); it can see and delete expired exports, nothing else.

Revision ID: 0033
Revises: 0032
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE TABLE export_job (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  requested_by  uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  kind          text NOT NULL
                CHECK (kind IN ('costs', 'recommendations', 'anomalies', 'budgets')),
  params        jsonb NOT NULL DEFAULT '{{}}',
  status        text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  progress      smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  row_count     integer,
  file_name     text NOT NULL,
  content       bytea,
  size_bytes    bigint,
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  expires_at    timestamptz NOT NULL DEFAULT now() + interval '7 days'
);
CREATE INDEX ON export_job (tenant_id, requested_by, created_at DESC);
CREATE INDEX ON export_job (expires_at);

ALTER TABLE export_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_job FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON export_job
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY scheduler_expire_read ON export_job FOR SELECT
  USING (app_scheduler() AND expires_at < now());
CREATE POLICY scheduler_expire_delete ON export_job FOR DELETE
  USING (app_scheduler() AND expires_at < now());
GRANT SELECT, INSERT, UPDATE, DELETE ON export_job TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS export_job;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
