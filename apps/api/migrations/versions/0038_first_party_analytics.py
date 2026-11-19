"""First-party analytics: site visits and the sign-up funnel (SITE-15, docs/14 §8.4).

No third-party analytics (founder decision, 19 Nov): the public site's beacon and the API's
funnel events are stored here, in our own database.

- ``site_event``: one anonymous row per page view or call-to-action click on the public
  site. No IP address, user agent, cookie or any identifier: a path, the referring host,
  the campaign (``utm_*``, ``ref``) and the hour it happened. Rows are kept 400 days; the
  beacon route deletes older rows now and then (``site_expire``).
- ``funnel_event``: the first time each step happens, one row per workspace (or per user
  for ``signup_completed``), with internal IDs only. ``workspace_created`` also carries the
  first-touch campaign the app sent with the new workspace. Deleted with the workspace.

Reads across workspaces need ``app.analytics_read = 'on'`` (``app_analytics_reader()``),
which only the staff funnel route sets after the platform-admin and MFA checks. A workspace
sees its own funnel rows (``tenant_isolation``); a new user may write their own sign-up row.

Revision ID: 0038
Revises: 0037
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE FUNCTION app_analytics_reader() RETURNS boolean LANGUAGE sql STABLE AS
  $$ SELECT coalesce(current_setting('app.analytics_read', true), '') = 'on' $$;
GRANT EXECUTE ON FUNCTION app_analytics_reader() TO {APP_ROLE};

CREATE TABLE site_event (
  id             bigserial PRIMARY KEY,
  -- Truncated to the hour: enough for daily and weekly counts, too coarse to follow a visit.
  occurred_at    timestamptz NOT NULL DEFAULT date_trunc('hour', now()),
  kind           text NOT NULL CHECK (kind IN ('page_view', 'cta_click')),
  path           text NOT NULL CHECK (length(path) BETWEEN 1 AND 200 AND left(path, 1) = '/'),
  -- An entry page: the visitor came from another site or typed the address.
  landing        boolean NOT NULL DEFAULT false,
  referrer_host  text CHECK (length(referrer_host) <= 253),
  cta            text CHECK (length(cta) <= 40),
  utm_source     text CHECK (length(utm_source) <= 100),
  utm_medium     text CHECK (length(utm_medium) <= 100),
  utm_campaign   text CHECK (length(utm_campaign) <= 100),
  utm_content    text CHECK (length(utm_content) <= 100),
  utm_term       text CHECK (length(utm_term) <= 100),
  ref            text CHECK (length(ref) <= 100)
);
CREATE INDEX ON site_event (occurred_at);

ALTER TABLE site_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE site_event FORCE ROW LEVEL SECURITY;
CREATE POLICY site_write ON site_event FOR INSERT WITH CHECK (true);
CREATE POLICY site_read ON site_event FOR SELECT USING (app_analytics_reader());
CREATE POLICY site_expire ON site_event FOR DELETE
  USING (app_analytics_reader() AND occurred_at < now() - interval '400 days');
GRANT SELECT, INSERT, DELETE ON site_event TO {APP_ROLE};
GRANT USAGE ON SEQUENCE site_event_id_seq TO {APP_ROLE};

CREATE TABLE funnel_event (
  id            bigserial PRIMARY KEY,
  event         text NOT NULL CHECK (event IN ('signup_completed', 'workspace_created',
                  'connect_started', 'connected', 'first_costs', 'first_recommendation')),
  tenant_id     uuid REFERENCES tenant(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES app_user(id) ON DELETE SET NULL,
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  detail        text CHECK (length(detail) <= 40),
  source        text CHECK (length(source) <= 100),
  medium        text CHECK (length(medium) <= 100),
  campaign      text CHECK (length(campaign) <= 100),
  content       text CHECK (length(content) <= 100),
  term          text CHECK (length(term) <= 100),
  ref           text CHECK (length(ref) <= 100),
  landing_path  text CHECK (length(landing_path) <= 200),
  CHECK ((event = 'signup_completed') = (tenant_id IS NULL))
);
-- Each step once: per workspace, and sign-up once per user.
CREATE UNIQUE INDEX funnel_event_once_per_tenant ON funnel_event (tenant_id, event)
  WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX funnel_event_signup_once ON funnel_event (user_id)
  WHERE event = 'signup_completed';
CREATE INDEX ON funnel_event (occurred_at);

ALTER TABLE funnel_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE funnel_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON funnel_event
  USING (tenant_id = app_current_tenant())
  WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY own_signup ON funnel_event FOR INSERT
  WITH CHECK (tenant_id IS NULL AND user_id = app_current_user());
CREATE POLICY analytics_read ON funnel_event FOR SELECT USING (app_analytics_reader());
GRANT SELECT, INSERT ON funnel_event TO {APP_ROLE};
GRANT USAGE ON SEQUENCE funnel_event_id_seq TO {APP_ROLE};
"""

DOWNGRADE = """
DROP TABLE IF EXISTS funnel_event, site_event;
DROP FUNCTION IF EXISTS app_analytics_reader();
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
