"""The platform audit chain links its rows (audit lead: platform rows under RLS).

Platform rows (``tenant_id IS NULL``: break-glass grants, exchange rates) are written by the
app role, and ``audit_read`` (``tenant_id = app_current_tenant()``) never shows them to it. So
``audit.record`` found no predecessor, every platform row was stored with ``prev_hash`` NULL,
and ``verify_chain(None)`` passed on the empty result it could see.

- ``audit_platform_head()``: the ``row_hash`` of the newest platform row, for the writer.
  SECURITY DEFINER with a fixed ``search_path``, owned by the table owner; it returns one hash
  and nothing else, so the app role still cannot read platform rows.
- ``audit_platform_owner``: FORCE row-level security binds the table owner too (``doadmin``
  on staging is not a superuser), so the owner, and the function running as it, may read
  platform rows. The owner can already disable RLS on its own table; this adds no power.
- ``audit_platform_console``: the staff console (``app.support_console = 'on'``, set only by
  the staff routes after the platform-admin and MFA checks) may read platform rows, so the
  app can verify the platform chain.

Rows written before this migration keep their hashes: ``verify_chain`` checks that unlinked
prefix row by row (see ``cloudoptics.audit.log``). Tenant chains are unchanged.

Revision ID: 0037
Revises: 0035
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0037"
down_revision = "0035"
branch_labels = None
depends_on = None

APP_ROLE = "cloudoptics_app"

UPGRADE = f"""
CREATE FUNCTION audit_platform_head() RETURNS bytea
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT row_hash FROM public.audit_log WHERE tenant_id IS NULL ORDER BY id DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION audit_platform_head() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit_platform_head() TO {APP_ROLE};

CREATE POLICY audit_platform_console ON audit_log FOR SELECT
  USING (tenant_id IS NULL AND app_support_console());

DO $$
DECLARE tbl_owner name := (SELECT tableowner FROM pg_tables
                       WHERE schemaname = 'public' AND tablename = 'audit_log');
BEGIN
  EXECUTE format('ALTER FUNCTION audit_platform_head() OWNER TO %I', tbl_owner);
  EXECUTE format('CREATE POLICY audit_platform_owner ON audit_log FOR SELECT TO %I '
                 'USING (tenant_id IS NULL)', tbl_owner);
END $$;
"""

DOWNGRADE = """
DROP POLICY IF EXISTS audit_platform_owner ON audit_log;
DROP POLICY IF EXISTS audit_platform_console ON audit_log;
DROP FUNCTION IF EXISTS audit_platform_head();
"""


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
