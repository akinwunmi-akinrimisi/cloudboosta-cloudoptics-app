"""Procrastinate job queue schema (ADR-003), with grants for the app role.

Later Procrastinate upgrades ship their own SQL migrations; add them as new revisions.

Revision ID: 0002
Revises: 0001
"""

from procrastinate.schema import SchemaManager

from cloudoptics.platform.migration_sql import run_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

GRANTS = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables
           WHERE schemaname = 'public' AND tablename LIKE 'procrastinate\\_%' LOOP
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO cloudoptics_app', r.tablename);
  END LOOP;
  FOR r IN SELECT sequencename FROM pg_sequences
           WHERE schemaname = 'public' AND sequencename LIKE 'procrastinate\\_%' LOOP
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I TO cloudoptics_app', r.sequencename);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\\_%' LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO cloudoptics_app', r.fn);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    run_sql(SchemaManager.get_schema())
    run_sql(GRANTS)


def downgrade() -> None:
    run_sql(
        r"""
        DROP TABLE IF EXISTS procrastinate_events, procrastinate_periodic_defers,
          procrastinate_jobs, procrastinate_workers CASCADE;
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
                   JOIN pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = 'public' AND p.proname LIKE 'procrastinate\_%' LOOP
            EXECUTE format('DROP FUNCTION %s CASCADE', r.fn);
          END LOOP;
        END $$;
        DROP TYPE IF EXISTS procrastinate_job_event_type, procrastinate_job_status,
          procrastinate_job_to_defer_v1 CASCADE;
        """
    )
