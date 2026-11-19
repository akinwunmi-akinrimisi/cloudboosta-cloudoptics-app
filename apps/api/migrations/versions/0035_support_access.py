"""Staff break-glass access to one workspace (WBS ADM-6, PRD FR-SEC-07, docs/11b §7.4).

- ``support_access_grant``: a Cloudoptics staff member's time-boxed, read-only access to one
  workspace, with the justification and the support ticket reference. Active while
  ``ended_at`` is null and ``expires_at`` is in the future. ``tenant_name`` is copied at
  grant time so the staff console can list grants without reading other workspaces.
- ``support_access_request``: every request made under a grant (method, path, status of the
  check), written on its own connection so it survives the request's read-only transaction.

Policies: the workspace's own scope (``tenant_isolation``); the staff member's own grants
(``/v1/me`` lists them, ``app_current_user()``); and the staff console
(``app.support_console = 'on'``, set only by the staff routes after the platform-admin and
MFA checks), which may read every grant and request but write none.

Revision ID: 0035
Revises: 0034
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE FUNCTION app_support_console() RETURNS boolean LANGUAGE sql STABLE AS
  $$ SELECT coalesce(current_setting('app.support_console', true), '') = 'on' $$;
GRANT EXECUTE ON FUNCTION app_support_console() TO {APP_ROLE};

CREATE TABLE support_access_grant (
  id             uuid PRIMARY KEY,
  tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  tenant_name    text NOT NULL,
  staff_user_id  uuid NOT NULL REFERENCES app_user(id),
  justification  text NOT NULL CHECK (length(justification) BETWEEN 10 AND 2000),
  ticket_ref     text NOT NULL CHECK (length(ticket_ref) BETWEEN 1 AND 100),
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  ended_at       timestamptz,
  ended_by       uuid REFERENCES app_user(id),
  CHECK (expires_at > created_at AND expires_at <= created_at + interval '2 hours')
);
CREATE INDEX ON support_access_grant (tenant_id, staff_user_id) WHERE ended_at IS NULL;
CREATE INDEX ON support_access_grant (staff_user_id, created_at DESC);
CREATE INDEX ON support_access_grant (created_at DESC);

CREATE TABLE support_access_request (
  id           bigserial PRIMARY KEY,
  grant_id     uuid NOT NULL REFERENCES support_access_grant(id) ON DELETE CASCADE,
  tenant_id    uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
  method       text NOT NULL,
  path         text NOT NULL,
  allowed      boolean NOT NULL,
  occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON support_access_request (grant_id, id DESC);
CREATE INDEX ON support_access_request (tenant_id);

ALTER TABLE support_access_grant ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_access_grant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON support_access_grant
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY staff_own_read ON support_access_grant FOR SELECT
  USING (staff_user_id = app_current_user());
CREATE POLICY support_console_read ON support_access_grant FOR SELECT
  USING (app_support_console());
GRANT SELECT, INSERT, UPDATE ON support_access_grant TO {APP_ROLE};

ALTER TABLE support_access_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_access_request FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON support_access_request
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY support_console_read ON support_access_request FOR SELECT
  USING (app_support_console());
GRANT SELECT, INSERT ON support_access_request TO {APP_ROLE};
GRANT USAGE ON SEQUENCE support_access_request_id_seq TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS support_access_request, support_access_grant;
DROP FUNCTION IF EXISTS app_support_console();
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
