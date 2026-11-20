"""Connecting Google Cloud (ADR-019): the customer's clicks, and the per-step check of a
connection.

**Telling workspaces apart.** Each connection has a Cloudoptics service account of its own
(``identity``), and every read for the connection is made as that account. The customer shares
the billing export with that account only, so typing someone else's dataset gets nothing: the
account has no access to it. A view is no way round it either: BigQuery runs a view with the
querying account's own access to the tables it reads (unless their owner authorised the view),
so a view over another customer's export reads nothing. This replaced the dataset label and its
view check (20 Nov 2026).

The export must still be for the billing account the connection names (``billing_account``),
and that billing account is reserved for the first workspace that proves it (migration 0017).
Projects come from the bill itself (``SubAccountId``), never from what a user types.

**Who pays for queries.** By default the queries run in Cloudoptics' own project
(``CLOUDOPTICS_GCP_QUERY_PROJECT``) and Cloudoptics pays for them, so the customer grants no
BigQuery Job User. A connection may name its own query project instead (then it needs Job
User there, and pays).

**The steps** the wizard shows (no commands):

1. Turn on the billing export (Google has no API for it).
2. Share the export's dataset with the connection's account as BigQuery Data Viewer.
3. Optional: grant the viewer roles once, on the organisation or a project.

The gcloud and bq commands are offered as an alternative (``commands``).
"""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analytics import funnel
from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.validation import Check
from cloudoptics.notifications.alerts import should_alert
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.gcp.client import (
    GcpApi,
    GcpError,
    NoServiceAccountError,
    NotConfiguredError,
    param,
)
from cloudoptics.providers.gcp.settings import get_gcp_settings

STACK_NAME = "gcp"
CONSOLE = "https://console.cloud.google.com"
#: The FOCUS columns the load reads; a table without them is not a FOCUS export.
REQUIRED_COLUMNS = (
    "BillingAccountId",
    "BillingPeriodStart",
    "ChargePeriodStart",
    "ChargeCategory",
    "BillingCurrency",
    "BilledCost",
    "EffectiveCost",
    "SubAccountId",
    "ServiceName",
)
#: What the inventory, metrics and Recommender need in each project (testIamPermissions).
PROJECT_PERMISSIONS = [
    "compute.instances.list",
    "compute.disks.list",
    "monitoring.timeSeries.list",
    "recommender.computeInstanceIdleResourceRecommendations.list",
]
#: Least privilege: the Compute and Cloud SQL Recommender viewers rather than Recommender
#: Viewer, which reads every recommender's findings (IAM, security, ...). All five can be
#: granted on a project, a folder or the organisation (inherited by its projects).
PROJECT_ROLES = (
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/recommender.computeViewer",
    "roles/recommender.cloudsqlViewer",
    "roles/cloudsql.viewer",
)
ROLE_TITLES = {
    "roles/bigquery.dataViewer": "BigQuery Data Viewer",
    "roles/bigquery.jobUser": "BigQuery Job User",
    "roles/compute.viewer": "Compute Viewer",
    "roles/monitoring.viewer": "Monitoring Viewer",
    "roles/recommender.computeViewer": "Compute Recommender Viewer",
    "roles/recommender.cloudsqlViewer": "Cloud SQL Recommender Viewer",
    "roles/cloudsql.viewer": "Cloud SQL Viewer",
}
PROJECTS_CHECKED = 20

SELECT_GCP = text(
    "SELECT connection_id, billing_account_id, export_project_id, export_dataset_id, "
    "export_table_id, export_kind, query_project_id, dataset_location, service_account_email "
    "FROM gcp_connection WHERE connection_id = :c"
)
SELECT_PROJECTS = text(
    "SELECT provider_account_id FROM cloud_account WHERE connection_id = :c AND provider = 'gcp' "
    "AND account_type = 'member' AND coalesce(org_status, 'ACTIVE') = 'ACTIVE' "
    "ORDER BY provider_account_id LIMIT :n"
)
STORE_RESULT = text(
    "UPDATE cloud_connection SET status = :status, health = :status, "
    "permission_checks = CAST(:checks AS jsonb), last_error = CAST(:err AS jsonb), "
    "last_validated_at = now(), account_verified_at = CASE WHEN :verified THEN "
    "coalesce(account_verified_at, now()) ELSE account_verified_at END "
    "WHERE id = :c AND status <> 'disconnected'"
)
RECORD_RUN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
    "finished_at, error_code, est_customer_api_cost_usd) VALUES (:id, :t, :c, "
    "'connection.validate', :status, now(), now(), :code, :cost)"
)
UPSERT_BILLING_ACCOUNT = text(
    "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
    "account_type) VALUES (:id, :t, :c, 'gcp', :acct, 'management') "
    "ON CONFLICT (tenant_id, provider, provider_account_id) DO NOTHING"
)


@dataclass(frozen=True)
class GcpSettingsRow:
    connection_id: uuid.UUID
    billing_account_id: str
    export_project_id: str
    export_dataset_id: str
    export_table_id: str
    export_kind: Literal["focus_export", "focus_view"]
    #: The customer's own query project, or None: Cloudoptics' (which pays).
    query_project_id: str | None
    dataset_location: str | None
    #: The connection's own Cloudoptics service account; None for a label-era connection.
    service_account_email: str | None = None

    @property
    def table_ref(self) -> str:
        return f"{self.export_project_id}.{self.export_dataset_id}.{self.export_table_id}"

    @property
    def customer_pays(self) -> bool:
        return bool(self.query_project_id) or not get_gcp_settings().our_query_project

    @property
    def query_project(self) -> str:
        """Where the queries run: the customer's project when they chose one, else
        Cloudoptics' (else, while ours is not set up, the export project)."""
        return (
            self.query_project_id or get_gcp_settings().our_query_project or self.export_project_id
        )


async def load_settings(session: AsyncSession, connection_id: uuid.UUID) -> GcpSettingsRow | None:
    row = (await session.execute(SELECT_GCP, {"c": connection_id})).one_or_none()
    return None if row is None else GcpSettingsRow(**row._mapping)


def api_for(gs: GcpSettingsRow) -> GcpApi:
    """The Google APIs as this connection's own service account (and nobody else's)."""
    if not gs.service_account_email:
        raise NoServiceAccountError()
    return GcpApi(service_account=gs.service_account_email, customer_pays=gs.customer_pays)


# --- what the customer does ---------------------------------------------------------------


def links(gs: GcpSettingsRow) -> dict[str, str]:
    """Console deep links for the wizard's steps (Google's documented console URLs)."""
    p, d = gs.export_project_id, gs.export_dataset_id
    return {
        # Billing, Billing export (the export setup guides link here).
        "billing_export": f"{CONSOLE}/billing/export",
        # The dataset's page in BigQuery Studio, where Share, Manage permissions is.
        "dataset": f"{CONSOLE}/bigquery?"
        + urlencode({"project": p, "p": p, "d": d, "page": "dataset"}),
        # IAM for one project, or the picker for an organisation, folder or project.
        "iam_project": f"{CONSOLE}/iam-admin/iam?" + urlencode({"project": p}),
        "iam_any": f"{CONSOLE}/projectselector/iam-admin/iam?"
        + urlencode({"supportedpurview": "project,folder,organizationId"}),
    }


def access_roles(gs: GcpSettingsRow) -> list[str]:
    """Step 3's roles: Job User only when the customer runs the queries themselves."""
    return (["roles/bigquery.jobUser"] if gs.customer_pays else []) + list(PROJECT_ROLES)


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    command: str
    optional: bool = False


def _q(value: str) -> str:
    return shlex.quote(value)


def commands(gs: GcpSettingsRow, *, service_account: str, projects: list[str]) -> list[Command]:
    """The same steps as commands, for those who prefer a terminal."""
    member = f"serviceAccount:{service_account}"
    location = f" --location={_q(gs.dataset_location)}" if gs.dataset_location else ""
    grant = (
        f"GRANT `roles/bigquery.dataViewer` ON SCHEMA "
        f'`{gs.export_project_id}.{gs.export_dataset_id}` TO "{member}"'
    )
    scanned = " ".join(_q(p) for p in (projects or [gs.export_project_id]))
    roles = " ".join(PROJECT_ROLES)
    out = [
        Command(
            "dataset",
            "Share the dataset (and, for a view, each dataset it reads)",
            f"bq query --nouse_legacy_sql{location} {_q(grant)}",
        )
    ]
    if gs.customer_pays:
        out.append(
            Command(
                "jobs",
                f"Let Cloudoptics run queries in {gs.query_project} (billed to that project)",
                f"gcloud projects add-iam-policy-binding {_q(gs.query_project)} "
                f"--member={_q(member)} --role=roles/bigquery.jobUser --condition=None",
            )
        )
    out.append(
        Command(
            "projects",
            "Optional: resources and recommendations (or once with gcloud organizations "
            "add-iam-policy-binding)",
            f"for p in {scanned}; do for r in {roles}; do gcloud projects "
            f'add-iam-policy-binding "$p" --member={_q(member)} --role="$r" '
            "--condition=None; done; done",
            optional=True,
        )
    )
    return out


# --- the check ---------------------------------------------------------------------------


@dataclass
class GcpValidation:
    """The provider protocol's ``ValidationOutcome`` for Google Cloud."""

    status: Literal["healthy", "degraded", "failed"]
    checks: list[Check]
    error: dict[str, str] | None = None
    est_cost_usd: float = 0.0
    has_billing_export: bool = False
    verified: bool = False
    health_worsened: bool = False

    def checks_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.checks]


@dataclass
class _Run:
    checks: list[Check] = field(default_factory=list)
    fatal: str | None = None  # the error code of a check that fails the connection
    errors: list[str] = field(default_factory=list)

    def ok(self, cid: str, label: str, message: str) -> None:
        self.checks.append(Check(cid, label, "ok", message))

    def warn(self, cid: str, label: str, message: str, hint: str | None = None) -> None:
        self.checks.append(Check(cid, label, "warning", message, hint))

    def fail(
        self, cid: str, label: str, message: str, hint: str | None, code: str, *, fatal: bool
    ) -> None:
        self.checks.append(Check(cid, label, "error", message, hint))
        if fatal and self.fatal is None:
            self.fatal = f"{code}:{cid}"
        self.errors.append(f"{code}:{cid}")


BILLING_ACCOUNTS_SQL = (
    "/* co:billing_accounts */ SELECT BillingAccountId AS billing_account_id, COUNT(*) AS n "
    "FROM `{table}` WHERE BillingPeriodStart >= TIMESTAMP_SUB(TIMESTAMP(@since), INTERVAL 1 DAY) "
    "GROUP BY 1 "
    "ORDER BY n DESC LIMIT 10"
)
SIGN_IN = "Sign in as your Cloudoptics account"
OUR_SIDE = "This is on Cloudoptics' side; try again in a few minutes, or contact support."


async def check_access(
    api: GcpApi, gs: GcpSettingsRow, projects: list[str], today: date
) -> GcpValidation:
    run = _Run()
    try:
        await api.token()
        run.ok("federation", SIGN_IN, f"Signed in as {api.service_account}.")
    except NotConfiguredError:
        run.fail(
            "federation",
            SIGN_IN,
            "Google Cloud connections are not switched on for this Cloudoptics environment.",
            "This is on Cloudoptics' side; contact support.",
            "GcpNotConfigured",
            fatal=True,
        )
        return _finish(run, api, export=False)
    except GcpError as exc:
        new = exc.not_found  # a just-created account takes up to a minute to appear
        run.fail(
            "federation",
            SIGN_IN,
            "Your Cloudoptics account is still being set up."
            if new
            else "Cloudoptics could not sign in to Google Cloud.",
            "Try again in a minute." if new else OUR_SIDE,
            exc.code,
            fatal=True,
        )
        return _finish(run, api, export=False)

    dataset_name = f"{gs.export_project_id}.{gs.export_dataset_id}"
    try:
        await api.dataset(gs.export_project_id, gs.export_dataset_id)
    except GcpError as exc:
        missing = exc.not_found
        run.fail(
            "dataset",
            "Read the billing export dataset",
            f"We can't find dataset {dataset_name}: check its name, or finish step 2."
            if missing
            else f"We can't see dataset {dataset_name} yet: finish step 2.",
            f"Share it with {api.service_account} as BigQuery Data Viewer.",
            exc.code,
            fatal=True,
        )
        return _finish(run, api, export=False)
    run.ok("dataset", "Read the billing export dataset", f"Dataset {dataset_name} is shared.")

    table_ok = False
    try:
        table = await api.table(gs.export_project_id, gs.export_dataset_id, gs.export_table_id)
        columns = {f.get("name") for f in (table.get("schema") or {}).get("fields") or []}
        missing_cols = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing_cols:
            run.fail(
                "export_table",
                "Find the FOCUS table",
                f"{gs.export_table_id} is not a FOCUS table: no {', '.join(missing_cols)}.",
                "Choose the managed FOCUS export table, or the FOCUS view over the detailed "
                "export.",
                "NotFocus",
                fatal=False,
            )
        else:
            table_ok = True
            run.ok("export_table", "Find the FOCUS table", f"{gs.export_table_id} has FOCUS rows.")
    except GcpError as exc:
        run.fail(
            "export_table",
            "Find the FOCUS table",
            f"We can't find {gs.export_table_id} in {dataset_name}: check the table name."
            if exc.not_found
            else f"We can't read {gs.export_table_id} yet: finish step 2.",
            "For the managed export, leave the table empty and we use Google's name.",
            exc.code,
            fatal=False,
        )

    query_ok = await _query_jobs(api, gs, run)
    if table_ok and query_ok:
        await _billing_account(api, gs, run, today)
    await _projects(api, projects, run)
    return _finish(run, api, export=table_ok and query_ok)


async def _query_jobs(api: GcpApi, gs: GcpSettingsRow, run: _Run) -> bool:
    label = "Run BigQuery queries"
    theirs = gs.customer_pays
    if theirs:
        missing = f"We can't run queries in {gs.query_project} yet: finish step 3."
        hint: str | None = f"Grant {api.service_account} BigQuery Job User on that project."
    else:
        missing, hint = "Cloudoptics cannot run its queries.", OUR_SIDE
    try:
        held = await api.test_permissions(gs.query_project, ["bigquery.jobs.create"])
    except GcpError as exc:
        run.fail(
            "query_jobs",
            label,
            f"We could not check project {gs.query_project}.",
            "Check the project ID." if theirs else OUR_SIDE,
            exc.code,
            fatal=False,
        )
        return False
    if "bigquery.jobs.create" not in held:
        run.fail("query_jobs", label, missing, hint, "AccessDenied", fatal=False)
        return False
    run.ok(
        "query_jobs",
        label,
        f"Queries run in {gs.query_project}, which pays for them."
        if theirs
        else "Queries run in Cloudoptics' project, at our cost.",
    )
    return True


async def _billing_account(api: GcpApi, gs: GcpSettingsRow, run: _Run, today: date) -> None:
    since = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    label = "Match the billing account"
    try:
        result = await api.query(
            gs.query_project,
            BILLING_ACCOUNTS_SQL.format(table=gs.table_ref),
            [param("since", "STRING", since.isoformat())],
            location=gs.dataset_location,
        )
    except GcpError as exc:
        run.fail(
            "billing_account",
            label,
            "We could not query the export: finish step 2 for every dataset it reads."
            if exc.denied
            else "We could not query the export.",
            exc.message[:200],
            exc.code,
            fatal=False,
        )
        return
    found = [str(r["billing_account_id"]) for r in result.rows if r.get("billing_account_id")]
    if not found:
        run.warn(
            "billing_account",
            label,
            "The export has no rows for this month or last month yet.",
            "A new export starts to fill within a few hours, and the first backfill can take "
            "up to five days; Cloudoptics loads it as it arrives.",
        )
    elif gs.billing_account_id in found:
        run.ok("billing_account", label, f"The export is billing account {gs.billing_account_id}.")
    else:
        run.fail(
            "billing_account",
            label,
            f"This export is for billing account {found[0]}, not {gs.billing_account_id}: "
            "check step 1.",
            "Connect again with the billing account the export is for.",
            "BillingAccountMismatch",
            fatal=True,
        )


async def _projects(api: GcpApi, projects: list[str], run: _Run) -> None:
    label = "Read resources and recommendations"
    if not projects:
        run.checks.append(
            Check(
                "projects",
                label,
                "skipped",
                "Checked once the first bill names your projects.",
            )
        )
        return
    granted: list[str] = []
    missing: list[str] = []
    for project in projects[:PROJECTS_CHECKED]:
        try:
            held = await api.test_permissions(project, PROJECT_PERMISSIONS)
        except GcpError:
            held = set()
        (granted if set(PROJECT_PERMISSIONS) <= held else missing).append(project)
    if not missing:
        run.ok("projects", label, f"All {len(granted)} projects checked are readable.")
    else:
        run.warn(
            "projects",
            label,
            f"{len(granted)} of {len(granted) + len(missing)} projects are readable; costs "
            "still load for all of them. To see the rest, finish step 3.",
            "Not readable yet: "
            + ", ".join(missing[:5])
            + (" and others." if len(missing) > 5 else "."),
        )


def _finish(run: _Run, api: GcpApi, *, export: bool) -> GcpValidation:
    if run.fatal:
        code, check = run.fatal.split(":", 1)
        status: Literal["healthy", "degraded", "failed"] = "failed"
        error: dict[str, str] | None = {"code": code, "check": check}
    elif run.errors:
        code, check = run.errors[0].split(":", 1)
        status, error = "degraded", {"code": code, "check": check}
    else:
        status, error = "healthy", None
    shared = any(c.id == "dataset" and c.status == "ok" for c in run.checks)
    return GcpValidation(
        status=status,
        checks=run.checks,
        error=error,
        est_cost_usd=api.customer_cost_usd,
        has_billing_export=export and status != "failed",
        verified=shared and status != "failed",
    )


# --- the provider's ``connect`` capability ------------------------------------------------


async def run_validation(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
) -> GcpValidation | None:
    """Validate one Google Cloud connection inside the caller's transaction (tenant scope
    set); None for a disconnected connection, as for AWS."""
    from cloudoptics.connectors.service import get_connection
    from cloudoptics.providers.gcp import identity

    conn = await get_connection(session, connection_id)
    if conn.status == "disconnected":
        return None
    gs = await load_settings(session, connection_id)
    if gs is None:
        return None
    unavailable: str | None = None
    if not gs.service_account_email:
        # Made while Google Cloud was switched off here: its account is made now.
        try:
            if await identity.ensure(session, tenant_id, connection_id, actor):
                gs = await load_settings(session, connection_id) or gs
        except HTTPException as exc:
            unavailable = str(exc.detail)
    projects = [
        r.provider_account_id
        for r in await session.execute(SELECT_PROJECTS, {"c": connection_id, "n": PROJECTS_CHECKED})
    ]
    if gs.service_account_email:
        result = await check_access(api_for(gs), gs, projects, date.today())
    elif unavailable:
        result = GcpValidation(
            "failed",
            [Check("federation", SIGN_IN, "error", unavailable, OUR_SIDE)],
            error={"code": "ServiceAccountUnavailable", "check": "federation"},
        )
    else:
        result = await check_access(GcpApi(), gs, projects, date.today())  # not switched on

    try:
        async with session.begin_nested():
            stored = await _store(session, connection_id, result, result.verified)
    except IntegrityError:
        result = GcpValidation(
            "failed",
            [
                Check(
                    "billing_account",
                    "Match the billing account",
                    "error",
                    "This billing account is already connected to another Cloudoptics workspace.",
                    "Disconnect it there first, or ask that workspace's admin to invite you.",
                )
            ],
            error={"code": "AccountInUse", "check": "billing_account"},
        )
        stored = await _store(session, connection_id, result, verified=False)
    if not stored:
        return None
    if result.status != "failed":
        await funnel.emit(session, "connected", tenant_id=tenant_id)
        await session.execute(
            UPSERT_BILLING_ACCOUNT,
            {"id": uuid7(), "t": tenant_id, "c": connection_id, "acct": gs.billing_account_id},
        )
    await session.execute(
        RECORD_RUN,
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": connection_id,
            "status": "failed" if result.status == "failed" else "succeeded",
            "code": (result.error or {}).get("code"),
            "cost": result.est_cost_usd,
        },
    )
    result.health_worsened = should_alert(conn.health, result.status)
    if result.status != conn.status:
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="connection.status_changed",
            target_type="cloud_connection",
            target_id=str(connection_id),
            before={"status": conn.status},
            after={"status": result.status},
        )
    return result


async def _store(
    session: AsyncSession, connection_id: uuid.UUID, result: GcpValidation, verified: bool
) -> bool:
    stored = await session.execute(
        STORE_RESULT,
        {
            "status": result.status,
            "checks": json.dumps(result.checks_json()),
            "err": json.dumps(result.error) if result.error else None,
            "verified": verified,
            "c": connection_id,
        },
    )
    return bool(getattr(stored, "rowcount", 0))
