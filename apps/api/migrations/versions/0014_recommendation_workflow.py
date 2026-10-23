"""Recommendation workflow and tenant rule settings (WBS AN-16 partial, AN-17, API-5).

- ``recommendation.assignee_id``: the member a recommendation is assigned to.
- ``recommendation_event``: its history, newest last: status changes, assignments and
  comments, with who did it.
- ``rule_setting``: one row per tenant with the rules it turned off and its thresholds.

Revision ID: 0014
Revises: 0013
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
ALTER TABLE recommendation ADD COLUMN assignee_id uuid REFERENCES app_user(id)
  ON DELETE SET NULL;
CREATE INDEX ON recommendation (tenant_id, assignee_id) WHERE assignee_id IS NOT NULL;

CREATE TABLE recommendation_event (
  id                uuid PRIMARY KEY,
  tenant_id         uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  recommendation_id uuid NOT NULL REFERENCES recommendation(id) ON DELETE CASCADE,
  actor_id          uuid REFERENCES app_user(id) ON DELETE SET NULL,
  kind              text NOT NULL CHECK (kind IN ('status_changed', 'assigned', 'commented')),
  from_status       text,
  to_status         text,
  assignee_id       uuid REFERENCES app_user(id) ON DELETE SET NULL,
  body              text CHECK (length(body) <= 2000),
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON recommendation_event (recommendation_id, created_at);

CREATE TABLE rule_setting (
  tenant_id  uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
  settings   jsonb NOT NULL DEFAULT '{{}}',
  updated_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE recommendation_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE recommendation_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recommendation_event
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT ON recommendation_event TO {APP_ROLE};

ALTER TABLE rule_setting ENABLE ROW LEVEL SECURITY;
ALTER TABLE rule_setting FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rule_setting
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
GRANT SELECT, INSERT, UPDATE ON rule_setting TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS rule_setting;
DROP TABLE IF EXISTS recommendation_event;
ALTER TABLE recommendation DROP COLUMN IF EXISTS assignee_id;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
