"""Row-level security on ``app_user`` (multi-cloud plan, Phase 0: tenant isolation).

Until now the app role could SELECT, INSERT and UPDATE every ``app_user`` row (0001), so one
query bug could list every customer's email. From here a session sees only:

- itself (``app.user_id``);
- the members of the workspace in context (``app.tenant_id``): member lists, assignees,
  invites, alert and report recipients;
- people who acted in that workspace (its audit log actors) or hold a break-glass grant for
  it: history keeps the name of a former member or of a Cloudoptics support engineer;
- the staff who entered exchange rates (shown with the public rate history);
- everyone, in the staff console (``app.support_console``, set only after the platform-admin
  and MFA checks; see 0035).

Writes: the app role may update only its own row, and only ``preferred_currency_view``.
INSERT is revoked: sign-in goes through ``app_user_sign_in()``, a SECURITY DEFINER function
with a fixed ``search_path``, owned by the table owner, which applies the email collision
policy of ``cloudoptics.identity.deps`` (it must see and update another subject's row).

RLS is ENABLEd but not FORCEd: the policies bind the app role, which owns no table (a test
checks), while the owner (migrations, the sign-in function, restore drills) is not limited.

Revision ID: 0041
Revises: 0038
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0041"
down_revision = "0038"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE INDEX audit_log_actor_tenant_idx ON audit_log (actor_id, tenant_id)
  WHERE actor_id IS NOT NULL;
CREATE INDEX fx_rate_entered_by_idx ON fx_rate (entered_by) WHERE entered_by IS NOT NULL;

ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;

CREATE POLICY app_user_visible ON app_user FOR SELECT USING (
  id = app_current_user()
  OR app_support_console()
  OR EXISTS (SELECT 1 FROM membership m
             WHERE m.user_id = app_user.id AND m.tenant_id = app_current_tenant())
  OR EXISTS (SELECT 1 FROM audit_log a
             WHERE a.actor_id = app_user.id AND a.tenant_id = app_current_tenant())
  OR EXISTS (SELECT 1 FROM support_access_grant g
             WHERE g.staff_user_id = app_user.id AND g.tenant_id = app_current_tenant())
  OR EXISTS (SELECT 1 FROM fx_rate r WHERE r.entered_by = app_user.id)
);
CREATE POLICY app_user_update_self ON app_user FOR UPDATE
  USING (id = app_current_user()) WITH CHECK (id = app_current_user());

REVOKE INSERT, UPDATE ON app_user FROM {APP_ROLE};
GRANT UPDATE (preferred_currency_view) ON app_user TO {APP_ROLE};

-- Just-in-time sign-in (see cloudoptics.identity.deps). outcome: 'ok' or 'email_in_use';
-- released: the user whose unverified email moved to this verified identity, if any;
-- created: the row is new (a first sign-in, for the sign-up funnel event).
CREATE FUNCTION app_user_sign_in(p_id uuid, p_sub text, p_email text, p_name text,
                                 p_verified boolean)
RETURNS TABLE (id uuid, email text, display_name text, outcome text, released uuid,
               created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
  holder record;
  freed uuid;
BEGIN
  SELECT u.id, u.email_verified INTO holder FROM public.app_user u
    WHERE u.email = p_email::citext AND u.auth_subject <> p_sub FOR UPDATE;
  IF FOUND THEN
    IF NOT p_verified OR holder.email_verified THEN
      RETURN QUERY SELECT NULL::uuid, NULL::text, NULL::text, 'email_in_use'::text, NULL::uuid,
        false;
      RETURN;
    END IF;
    UPDATE public.app_user u
      SET email = 'released+' || holder.id::text || '@email-released.invalid',
          email_verified = false
      WHERE u.id = holder.id;
    freed := holder.id;
  END IF;
  BEGIN
    RETURN QUERY
      INSERT INTO public.app_user AS u (id, email, display_name, auth_subject, email_verified)
      VALUES (p_id, p_email, p_name, p_sub, p_verified)
      ON CONFLICT (auth_subject) DO UPDATE SET email = EXCLUDED.email,
        email_verified = EXCLUDED.email_verified,
        display_name = COALESCE(EXCLUDED.display_name, u.display_name)
      RETURNING u.id, u.email::text, u.display_name, 'ok'::text, freed, (u.xmax = 0);
  EXCEPTION WHEN unique_violation THEN
    -- Another sign-in took the email between the check and the insert.
    RETURN QUERY SELECT NULL::uuid, NULL::text, NULL::text, 'email_in_use'::text, freed, false;
  END;
END $$;
REVOKE ALL ON FUNCTION app_user_sign_in(uuid, text, text, text, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_user_sign_in(uuid, text, text, text, boolean) TO {APP_ROLE};

DO $$
DECLARE tbl_owner name := (SELECT tableowner FROM pg_tables
                           WHERE schemaname = 'public' AND tablename = 'app_user');
BEGIN
  EXECUTE format('ALTER FUNCTION app_user_sign_in(uuid, text, text, text, boolean) OWNER TO %I',
                 tbl_owner);
END $$;
"""

DOWNGRADE = f"""
DROP FUNCTION IF EXISTS app_user_sign_in(uuid, text, text, text, boolean);
REVOKE UPDATE (preferred_currency_view) ON app_user FROM {APP_ROLE};
GRANT INSERT, UPDATE ON app_user TO {APP_ROLE};
DROP POLICY IF EXISTS app_user_update_self ON app_user;
DROP POLICY IF EXISTS app_user_visible ON app_user;
ALTER TABLE app_user DISABLE ROW LEVEL SECURITY;
DROP INDEX IF EXISTS fx_rate_entered_by_idx;
DROP INDEX IF EXISTS audit_log_actor_tenant_idx;
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
