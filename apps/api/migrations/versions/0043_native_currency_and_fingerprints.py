"""Provider-neutral core, part 2: native billing currency and provider-scoped fingerprints
(ADR-018, multi-cloud plan Phase 1). Nothing changes for AWS data.

- The cost aggregates keep the bill's own currency next to USD: ``billing_currency``
  (ISO 4217, default ``USD``) and native amounts beside each ``*_usd`` one:
  ``cost_daily`` (``billed_cost``, ``effective_cost``, ``net_cost``, ``list_cost``),
  ``cost_resource_daily`` (``effective_cost``) and ``cost_snapshot_ce`` (``effective_cost``,
  ``billed_cost``, ``credits``). The internal reporting base stays USD (docs/09 §2.3.5).
  A BEFORE trigger sets native = USD on every row billed in USD, so AWS writers need no
  change and the two can never disagree; a row in another currency must bring its native
  amounts (NOT NULL, or NULL together with the USD one for ``list_cost``, which may be
  unknown). Existing rows are filled the same way.
- Recommendation fingerprints start with the provider: ``aws:R1_unattached_ebs:<account>:
  <region>:<resource>``. The engine writes the same form, so a re-run updates each row in
  place and statuses, assignees, history and realised savings stay where they are.

Data fixes use the NO FORCE / fix / FORCE pattern (the migration role is bound by FORCE RLS).
The downgrade strips the prefix and drops the columns; it refuses to run while a row is in
another currency.

Revision ID: 0043
Revises: 0042
"""

from cloudoptics.platform.migration_sql import run_sql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

# table -> native columns (each beside <column>_usd)
NATIVE: dict[str, tuple[str, ...]] = {
    "cost_daily": ("billed_cost", "effective_cost", "net_cost", "list_cost"),
    "cost_resource_daily": ("effective_cost",),
    "cost_snapshot_ce": ("effective_cost", "billed_cost", "credits"),
}
# Native columns whose USD column may be NULL (no list price known): NULL together.
NULLABLE = {("cost_daily", "list_cost")}


def _native(table: str, cols: tuple[str, ...]) -> str:
    """Columns, the USD trigger, the fill of existing rows, then the NOT NULL rules."""
    add = "".join(f"ALTER TABLE {table} ADD COLUMN {c} numeric(18, 6);\n" for c in cols)
    sets = "".join(f"    NEW.{c} := NEW.{c}_usd;\n" for c in cols)
    rules = "".join(
        f"ALTER TABLE {table} ADD CONSTRAINT {table}_{c}_check\n"
        f"  CHECK (({c} IS NULL) = ({c}_usd IS NULL));\n"
        if (table, c) in NULLABLE
        else f"ALTER TABLE {table} ALTER COLUMN {c} SET NOT NULL;\n"
        for c in cols
    )
    return f"""
ALTER TABLE {table} ADD COLUMN billing_currency char(3) NOT NULL DEFAULT 'USD'
  CONSTRAINT {table}_billing_currency_check CHECK (billing_currency ~ '^[A-Z]{{3}}$');
{add}CREATE FUNCTION {table}_native_usd() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.billing_currency = 'USD' THEN
{sets}  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER native_usd BEFORE INSERT OR UPDATE ON {table}
  FOR EACH ROW EXECUTE FUNCTION {table}_native_usd();
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
UPDATE {table} SET billing_currency = 'USD';
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
{rules}"""


UPGRADE = "".join(_native(t, cols) for t, cols in NATIVE.items())

UPGRADE += """
ALTER TABLE recommendation NO FORCE ROW LEVEL SECURITY;
UPDATE recommendation SET fingerprint = provider || ':' || fingerprint
  WHERE fingerprint NOT LIKE provider || ':%';
ALTER TABLE recommendation FORCE ROW LEVEL SECURITY;
"""

DOWNGRADE = """
ALTER TABLE recommendation NO FORCE ROW LEVEL SECURITY;
UPDATE recommendation SET fingerprint = substr(fingerprint, length(provider) + 2)
  WHERE fingerprint LIKE provider || ':%';
ALTER TABLE recommendation FORCE ROW LEVEL SECURITY;

DO $$
DECLARE
  t text;
  n bigint;
BEGIN
  FOREACH t IN ARRAY ARRAY['cost_daily', 'cost_resource_daily', 'cost_snapshot_ce'] LOOP
    EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('SELECT count(*) FROM %I WHERE billing_currency <> ''USD''', t) INTO n;
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    IF n > 0 THEN
      RAISE EXCEPTION '0043 downgrade: % has % rows not billed in USD', t, n;
    END IF;
  END LOOP;
END $$;
"""
for _t, _cols in NATIVE.items():
    DOWNGRADE += (
        f"DROP TRIGGER native_usd ON {_t};\n"
        f"DROP FUNCTION {_t}_native_usd();\n"
        + "".join(f"ALTER TABLE {_t} DROP COLUMN {c};\n" for c in _cols)
        + f"ALTER TABLE {_t} DROP COLUMN billing_currency;\n"
    )


def upgrade() -> None:
    run_sql(UPGRADE)


def downgrade() -> None:
    run_sql(DOWNGRADE)
