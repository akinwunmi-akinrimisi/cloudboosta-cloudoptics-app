"""RLS migration lint: the database after all migrations, checked against the isolation rules.

A new table, policy or role grant that weakens tenant isolation fails CI here until it is
fixed or listed below with a reason (docs/09 §2.3, multi-cloud plan Phase 0).

Rules:

1. Every table with a ``tenant_id`` column (and ``tenant`` itself) has row-level security
   ENABLEd and FORCEd, a policy whose USING has ``tenant_id = app_current_tenant()``, and every
   policy the app role can write through checks ``app_current_tenant()``.
2. Every other policy the app role is bound by (a cross-tenant read, a console, the
   scheduler) is listed in ``CROSS_TENANT_POLICIES`` with its reason.
3. Every table the app role can touch is a tenant table, ``app_user`` (its own rules,
   migration 0041) or listed in ``SHARED_TABLES``.
4. The app role is not a superuser, has no BYPASSRLS (directly or through a role it is a
   member of) and owns nothing, so policies always bind it, FORCE or not.
5. SECURITY DEFINER functions set a fixed ``search_path``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

Sql = Callable[..., list[tuple[Any, ...]]]

APP_ROLE = "cloudoptics_app"

# Tables without tenant data, so without RLS.
SHARED_TABLES: dict[str, str] = {
    "alembic_version": "migration bookkeeping; the app role has no privilege on it",
    "price_catalog": "public AWS list prices, the same for every workspace (read-only)",
    "fx_rate": "platform exchange rates (feed and staff-entered), not workspace data",
    "fx_data_version": "one platform-wide version counter for exchange rates",
    "rate_limit_counter": "throttling counters keyed by a hash; no workspace content",
    "site_event": (
        "anonymous public-site page views (0038): no tenant, user or IP; RLS lets the app "
        "insert, and only the staff analytics route read and expire"
    ),
    "procrastinate_jobs": "job queue: arguments carry IDs only; each job sets its tenant scope",
    "procrastinate_events": "job queue bookkeeping",
    "procrastinate_periodic_defers": "job queue bookkeeping",
    "procrastinate_workers": "job queue bookkeeping",
}

# Tables with tenant_id that do not follow rule 1, and why that is safe.
TENANT_EXCEPTIONS: dict[str, str] = {
    "data_version_pending": (
        "no RLS: the app role has no privilege on it; only the SECURITY DEFINER data-version "
        "triggers write it (0031) and it holds tenant IDs only"
    ),
    "tenant_data_version": (
        "RLS without FORCE: the SECURITY DEFINER triggers write it as the owner; the app role "
        "may only SELECT, through tenant_isolation"
    ),
}

# Policies binding the app role that are not plain tenant isolation: (table, policy) -> why.
CROSS_TENANT_POLICIES: dict[tuple[str, str], str] = {
    ("tenant", "tenant_visible"): "a user lists the workspaces they are a member of",
    ("membership", "membership_visible"): "a user sees their own memberships (workspace list)",
    ("invite", "addressed_to_me"): "a user sees invites addressed to their own email",
    ("cloud_connection", "scheduler_read"): "scheduled fan-out lists connections (0003)",
    ("data_deletion", "scheduler_read"): "the deletion job finds due workspaces",
    ("export_job", "scheduler_expire_read"): "the cleanup job finds expired exports",
    ("export_job", "scheduler_expire_delete"): "the cleanup job deletes expired exports",
    ("support_access_grant", "staff_own_read"): "staff see their own break-glass grants",
    ("support_access_grant", "support_console_read"): "staff console, after admin+MFA checks",
    ("support_access_request", "support_console_read"): "staff console, after admin+MFA checks",
    ("audit_log", "audit_append"): "platform rows (tenant_id NULL) are appended by the app",
    ("audit_log", "audit_platform_console"): "staff console verifies the platform chain (0037)",
    ("funnel_event", "own_signup"): (
        "a new user writes their own sign-up row, which has no workspace (tenant_id NULL, 0038)"
    ),
    ("funnel_event", "analytics_read"): "staff funnel report, after admin+MFA checks (0038)",
}

TENANT_QUAL = re.compile(r"^\(?(tenant_id|id) = app_current_tenant\(\)\)?$")
# A tenant-scoped term inside a wider USING (the extra branch is rule 2's business).
TENANT_TERM = re.compile(r"\b(tenant_id|id) = app_current_tenant\(\)")

TABLES_SQL = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
       EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
               AND a.attname = 'tenant_id' AND NOT a.attisdropped)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
"""
POLICIES_SQL = """
SELECT tablename, policyname, cmd, roles::text[], qual, with_check FROM pg_policies
WHERE schemaname = 'public'
"""
APP_TABLES_SQL = """
SELECT DISTINCT table_name FROM information_schema.role_table_grants
WHERE grantee = %s AND table_schema = 'public'
UNION
SELECT DISTINCT table_name FROM information_schema.role_column_grants
WHERE grantee = %s AND table_schema = 'public'
"""
APP_PRIVILEGES_SQL = """
SELECT privilege_type FROM information_schema.role_table_grants
WHERE grantee = %s AND table_schema = 'public' AND table_name = %s
"""
ROLE_SQL = """
WITH RECURSIVE granted(oid) AS (
  SELECT oid FROM pg_roles WHERE rolname = %s
  UNION SELECT m.roleid FROM pg_auth_members m JOIN granted g ON m.member = g.oid
)
SELECT r.rolname, r.rolsuper, r.rolbypassrls FROM pg_roles r JOIN granted g ON g.oid = r.oid
"""
OWNED_SQL = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(c.relowner) = %s
UNION ALL
SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) = %s
"""
DEFINER_SQL = """
SELECT p.oid::regprocedure::text, p.proconfig FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.prosecdef
"""


def _tables(owner_sql: Sql) -> dict[str, tuple[bool, bool, bool]]:
    return {r[0]: (r[1], r[2], r[3]) for r in owner_sql(TABLES_SQL)}


def _app_policies(owner_sql: Sql) -> list[tuple[str, str, str, str | None, str | None]]:
    """Policies that bind the app role (PUBLIC or the role itself)."""
    return [
        (table, name, cmd, qual, check)
        for table, name, cmd, roles, qual, check in owner_sql(POLICIES_SQL)
        if "public" in roles or APP_ROLE in roles
    ]


def _tenant_tables(owner_sql: Sql) -> set[str]:
    return {name for name, (_, _, has) in _tables(owner_sql).items() if has} | {"tenant"}


def test_tenant_tables_enable_and_force_rls_with_a_tenant_policy(owner_sql: Sql) -> None:
    tables = _tables(owner_sql)
    policies = _app_policies(owner_sql)
    problems = []
    for name in sorted(_tenant_tables(owner_sql) - set(TENANT_EXCEPTIONS)):
        enabled, forced, _ = tables[name]
        if not (enabled and forced):
            problems.append(f"{name}: RLS enabled={enabled} forced={forced}")
        own = [p for p in policies if p[0] == name]
        if not any(
            cmd in ("ALL", "SELECT") and qual and TENANT_TERM.search(qual)
            for _, _, cmd, qual, _ in own
        ):
            problems.append(f"{name}: no policy with tenant_id = app_current_tenant()")
        for _, policy, cmd, qual, check in own:
            if (name, policy) in CROSS_TENANT_POLICIES:
                continue  # reviewed: rule 2
            write_check = check if check is not None else qual
            if cmd in ("ALL", "INSERT", "UPDATE") and "app_current_tenant()" not in (
                write_check or ""
            ):
                problems.append(f"{name}.{policy}: {cmd} without app_current_tenant() check")
    assert problems == [], "\n".join(problems)


def test_every_other_policy_is_a_listed_exception(owner_sql: Sql) -> None:
    tenant_tables = _tenant_tables(owner_sql)
    unlisted = []
    for table, policy, _, qual, check in _app_policies(owner_sql):
        if table not in tenant_tables or table in TENANT_EXCEPTIONS:
            continue
        plain = (qual is None or TENANT_QUAL.match(qual)) and (
            check is None or TENANT_QUAL.match(check)
        )
        if not plain and (table, policy) not in CROSS_TENANT_POLICIES:
            unlisted.append(f"{table}.{policy}: USING {qual} WITH CHECK {check}")
    assert unlisted == [], "review and list in CROSS_TENANT_POLICIES:\n" + "\n".join(unlisted)
    present = {(t, p) for t, p, *_ in _app_policies(owner_sql)}
    assert set(CROSS_TENANT_POLICIES) - present == set(), "stale CROSS_TENANT_POLICIES entry"


def test_every_table_the_app_role_touches_is_isolated_or_listed(owner_sql: Sql) -> None:
    tables = _tables(owner_sql)
    tenant_tables = _tenant_tables(owner_sql)
    reachable = {r[0] for r in owner_sql(APP_TABLES_SQL, APP_ROLE, APP_ROLE)}
    unknown = reachable - tenant_tables - set(SHARED_TABLES) - {"app_user"}
    assert unknown == set(), f"add RLS or list in SHARED_TABLES: {sorted(unknown)}"
    # Shared tables really hold no tenant data.
    assert {t for t in SHARED_TABLES if t in tables and tables[t][2]} == set()
    assert set(SHARED_TABLES) - set(tables) == set(), "stale SHARED_TABLES entry"
    # app_user: RLS on, sign-in only through app_user_sign_in() (0041).
    assert tables["app_user"][0] is True
    assert set(r[0] for r in owner_sql(APP_PRIVILEGES_SQL, APP_ROLE, "app_user")) == {"SELECT"}


def test_tenant_exceptions_stay_narrow(owner_sql: Sql) -> None:
    privileges = {
        t: {r[0] for r in owner_sql(APP_PRIVILEGES_SQL, APP_ROLE, t)} for t in TENANT_EXCEPTIONS
    }
    assert privileges == {"data_version_pending": set(), "tenant_data_version": {"SELECT"}}
    tables = _tables(owner_sql)
    assert tables["tenant_data_version"][0] is True


def test_app_role_cannot_bypass_rls(owner_sql: Sql) -> None:
    roles = owner_sql(ROLE_SQL, APP_ROLE)
    assert roles, "the app role exists"
    assert [r for r in roles if r[1] or r[2]] == [], "superuser or BYPASSRLS reachable"
    # Policies bind the app role without FORCE only because it owns nothing.
    assert owner_sql(OWNED_SQL, APP_ROLE, APP_ROLE) == []


def test_security_definer_functions_fix_their_search_path(owner_sql: Sql) -> None:
    loose = [
        fn
        for fn, config in owner_sql(DEFINER_SQL)
        if not any(c.startswith("search_path=") for c in (config or []))
    ]
    assert loose == []
