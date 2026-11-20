"""Connecting Google Cloud (ADR-019): the customer's grants, the ownership proof, and the
per-permission check of a connection.

Every customer grants roles to the same Cloudoptics service account, so the account alone
cannot tell workspaces apart: anyone could type someone else's dataset into Cloudoptics.
The connection's **label value** plays the part of AWS's External ID. It is random, stored
encrypted (``cloud_connection.external_id_enc``) and shown only to the workspace's admins;
the customer sets it as the label ``cloudoptics`` on the dataset Cloudoptics reads, which
only someone who can change that dataset can do (``bigquery.datasets.update``: BigQuery Data
Owner or Admin). Cloudoptics reads no bill until the label matches, and only then reserves
the billing account for the workspace (migration 0017).

A label on the dataset that holds a **view** proves nothing about the data the view reads:
anyone can write a view over another customer's export, and BigQuery would run it with the
service account's own access. So when the table is not a plain table, a dry run lists the
base tables the view reads (``referencedTables``) and every dataset among them must carry the
same label; until then the check fails (``OwnershipNotProven``) and nothing is read.

Projects come from the bill itself (``SubAccountId``), never from what a user types, so a
workspace scans only projects billed to its own proven billing account.
"""

from __future__ import annotations

import json
import secrets
import shlex
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

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
    NotConfiguredError,
    bq_cost_usd,
    param,
)
from cloudoptics.providers.gcp.settings import get_gcp_settings

LABEL_KEY = "cloudoptics"
STACK_NAME = "gcp"
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
#: Viewer, which reads every recommender's findings (IAM, security, ...).
PROJECT_ROLES = (
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/recommender.computeViewer",
    "roles/recommender.cloudsqlViewer",
    "roles/cloudsql.viewer",
)
#: Table types read directly; anything else (a view) must prove its sources (module doc).
PLAIN_TABLES = frozenset({"TABLE"})
SOURCES_SQL = "/* co:sources */ SELECT 1 FROM `{table}` LIMIT 1"
PROJECTS_CHECKED = 20

SELECT_GCP = text(
    "SELECT connection_id, billing_account_id, export_project_id, export_dataset_id, "
    "export_table_id, export_kind, query_project_id, dataset_location FROM gcp_connection "
    "WHERE connection_id = :c"
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
    query_project_id: str
    dataset_location: str | None

    @property
    def table_ref(self) -> str:
        return f"{self.export_project_id}.{self.export_dataset_id}.{self.export_table_id}"


async def load_settings(session: AsyncSession, connection_id: uuid.UUID) -> GcpSettingsRow | None:
    row = (await session.execute(SELECT_GCP, {"c": connection_id})).one_or_none()
    return None if row is None else GcpSettingsRow(**row._mapping)


def new_label_value() -> str:
    """32 lowercase hex characters: a valid label value (128 random bits)."""
    return secrets.token_hex(16)


# --- what the customer runs -------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    command: str
    optional: bool = False


def _q(value: str) -> str:
    return shlex.quote(value)


def commands(
    gs: GcpSettingsRow, *, service_account: str, label: str, projects: list[str]
) -> list[Command]:
    member = f"serviceAccount:{service_account}"
    dataset = f"{gs.export_project_id}:{gs.export_dataset_id}"
    location = f" --location={_q(gs.dataset_location)}" if gs.dataset_location else ""
    grant = (
        f"GRANT `roles/bigquery.dataViewer` ON SCHEMA "
        f'`{gs.export_project_id}.{gs.export_dataset_id}` TO "{member}"'
    )
    scanned = " ".join(_q(p) for p in (projects or [gs.export_project_id]))
    roles = " ".join(PROJECT_ROLES)
    label_title = (
        "Label the dataset, and each dataset the view reads (proves they are yours)"
        if gs.export_kind == "focus_view"
        else "Label the dataset (proves it is yours)"
    )
    return [
        Command("label", label_title, f"bq update --set_label {LABEL_KEY}:{label} {_q(dataset)}"),
        Command(
            "dataset",
            "Let Cloudoptics read the dataset",
            f"bq query --nouse_legacy_sql{location} {_q(grant)}",
        ),
        Command(
            "jobs",
            f"Let Cloudoptics run queries in {gs.query_project_id} (billed to that project)",
            f"gcloud projects add-iam-policy-binding {_q(gs.query_project_id)} "
            f"--member={_q(member)} --role=roles/bigquery.jobUser --condition=None",
        ),
        Command(
            "projects",
            "Optional: resources and recommendations (list every project ID)",
            f"for p in {scanned}; do for r in {roles}; do gcloud projects "
            f'add-iam-policy-binding "$p" --member={_q(member)} --role="$r" '
            "--condition=None; done; done",
            optional=True,
        ),
    ]


def console_steps(gs: GcpSettingsRow, *, service_account: str, label: str) -> list[str]:
    view = (
        " If the table is a view, add the same label to every dataset the view reads, such "
        "as the detailed and pricing export datasets."
        if gs.export_kind == "focus_view"
        else ""
    )
    return [
        f"In BigQuery, open dataset {gs.export_dataset_id} in project {gs.export_project_id}, "
        f"choose Edit details, add the label {LABEL_KEY} with the value {label}, and save." + view,
        f"On the same dataset choose Sharing, then Permissions, then Add principal: "
        f"{service_account} with the role BigQuery Data Viewer.",
        f"In IAM and admin for project {gs.query_project_id}, choose Grant access: "
        f"{service_account} with the role BigQuery Job User.",
        "Optional, for resources and recommendations: in each project (or once on a folder or "
        "the organization) grant the same account Compute Viewer, Monitoring Viewer, "
        "Compute Recommender Viewer, Cloud SQL Recommender Viewer and Cloud SQL Viewer.",
    ]


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


async def check_access(
    api: GcpApi, gs: GcpSettingsRow, label: str, projects: list[str], today: date
) -> GcpValidation:
    run = _Run()
    try:
        await api.token()
        run.ok("federation", "Sign in to Google Cloud as Cloudoptics", "Signed in.")
    except NotConfiguredError:
        run.fail(
            "federation",
            "Sign in to Google Cloud as Cloudoptics",
            "Google Cloud connections are not switched on for this Cloudoptics environment.",
            "This is on Cloudoptics' side; contact support.",
            "GcpNotConfigured",
            fatal=True,
        )
        return _finish(run, api, export=False)
    except GcpError as exc:
        run.fail(
            "federation",
            "Sign in to Google Cloud as Cloudoptics",
            "Cloudoptics could not sign in to Google Cloud.",
            "This is on Cloudoptics' side; try again in a few minutes.",
            exc.code,
            fatal=True,
        )
        return _finish(run, api, export=False)

    dataset_name = f"{gs.export_project_id}:{gs.export_dataset_id}"
    try:
        dataset = await api.dataset(gs.export_project_id, gs.export_dataset_id)
    except GcpError as exc:
        missing = exc.not_found
        run.fail(
            "dataset",
            "Read the billing export dataset",
            f"Dataset {dataset_name} was not found."
            if missing
            else f"Cloudoptics cannot read dataset {dataset_name}.",
            "Check the project and dataset names."
            if missing
            else "Run the command that grants BigQuery Data Viewer on the dataset.",
            exc.code,
            fatal=True,
        )
        return _finish(run, api, export=False)
    run.ok("dataset", "Read the billing export dataset", f"Dataset {dataset_name} is readable.")

    labels = dataset.get("labels") or {}
    if not secrets.compare_digest(str(labels.get(LABEL_KEY, "")), label):
        run.fail(
            "ownership",
            "Prove the dataset is yours",
            f"The dataset has no {LABEL_KEY} label with this connection's value.",
            "Run the first command (bq update --set_label ...) exactly as shown.",
            "OwnershipNotProven",
            fatal=True,
        )
        return _finish(run, api, export=False)
    run.ok("ownership", "Prove the dataset is yours", "The label matches this workspace.")

    table_ok = False
    table_type = "TABLE"
    try:
        table = await api.table(gs.export_project_id, gs.export_dataset_id, gs.export_table_id)
        table_type = str(table.get("type") or "TABLE")
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
            f"Table or view {gs.export_table_id} was not found in {dataset_name}."
            if exc.not_found
            else f"Cloudoptics cannot read {gs.export_table_id}.",
            "Check the table name; for the managed export, the table Google created.",
            exc.code,
            fatal=False,
        )

    query_ok = False
    try:
        held = await api.test_permissions(gs.query_project_id, ["bigquery.jobs.create"])
        if "bigquery.jobs.create" not in held:
            run.fail(
                "query_jobs",
                "Run BigQuery queries",
                f"Cloudoptics cannot run queries in project {gs.query_project_id}.",
                "Run the command that grants BigQuery Job User on that project.",
                "AccessDenied",
                fatal=False,
            )
        else:
            query_ok = True
            run.ok(
                "query_jobs",
                "Run BigQuery queries",
                f"Queries run in {gs.query_project_id}, which pays for them.",
            )
    except GcpError as exc:
        run.fail(
            "query_jobs",
            "Run BigQuery queries",
            f"Cloudoptics could not check project {gs.query_project_id}.",
            "Check the project ID.",
            exc.code,
            fatal=False,
        )

    sources_ok = True
    if table_ok and query_ok and table_type not in PLAIN_TABLES:
        sources_ok = await _view_sources(api, gs, label, run)
        if not sources_ok:
            return _finish(run, api, export=False)
    if table_ok and query_ok:
        await _billing_account(api, gs, run, today)
    await _projects(api, projects, run)
    return _finish(run, api, export=table_ok and query_ok and sources_ok)


async def _view_sources(api: GcpApi, gs: GcpSettingsRow, label: str, run: _Run) -> bool:
    """A view: every dataset it reads must carry this connection's label (module doc)."""
    name = "Prove the view's sources are yours"
    hint = "Add the label from the first command to each dataset the view reads."
    try:
        tables = await api.referenced_tables(
            gs.query_project_id,
            SOURCES_SQL.format(table=gs.table_ref),
            location=gs.dataset_location,
        )
    except GcpError as exc:
        run.fail(
            "view_sources",
            name,
            "Cloudoptics could not list the tables the view reads.",
            exc.message[:200],
            exc.code,
            fatal=True,
        )
        return False
    datasets = sorted({(p, d) for p, d, _ in tables})
    if not datasets:
        run.fail(
            "view_sources",
            name,
            "BigQuery did not say which tables the view reads.",
            hint,
            "OwnershipNotProven",
            fatal=True,
        )
        return False
    unproven: list[str] = []
    for project, dataset in datasets:
        if (project, dataset) == (gs.export_project_id, gs.export_dataset_id):
            continue  # checked above
        try:
            labels = (await api.dataset(project, dataset)).get("labels") or {}
        except GcpError:
            labels = {}
        if not secrets.compare_digest(str(labels.get(LABEL_KEY, "")), label):
            unproven.append(f"{project}:{dataset}")
    if unproven:
        run.fail(
            "view_sources",
            name,
            f"The view reads datasets without this connection's label: {', '.join(unproven[:5])}.",
            hint,
            "OwnershipNotProven",
            fatal=True,
        )
        return False
    run.ok("view_sources", name, f"All {len(datasets)} datasets the view reads carry the label.")
    return True


async def _billing_account(api: GcpApi, gs: GcpSettingsRow, run: _Run, today: date) -> None:
    since = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    label = "Match the billing account"
    try:
        result = await api.query(
            gs.query_project_id,
            BILLING_ACCOUNTS_SQL.format(table=gs.table_ref),
            [param("since", "STRING", since.isoformat())],
            location=gs.dataset_location,
        )
    except GcpError as exc:
        run.fail(
            "billing_account",
            label,
            "Cloudoptics could not query the export.",
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
            f"The export belongs to billing account {found[0]}, not {gs.billing_account_id}.",
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
            "still load for all of them.",
            "Run the optional command for: "
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
    ownership = any(c.id == "ownership" and c.status == "ok" for c in run.checks)
    return GcpValidation(
        status=status,
        checks=run.checks,
        error=error,
        est_cost_usd=bq_cost_usd(api.bytes_billed),
        has_billing_export=export and status != "failed",
        verified=ownership and status != "failed",
    )


# --- the provider's ``connect`` capability ------------------------------------------------


async def run_validation(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
) -> GcpValidation | None:
    """Validate one Google Cloud connection inside the caller's transaction (tenant scope
    set); None for a disconnected connection, as for AWS."""
    from cloudoptics.connectors.service import get_connection, read_external_id

    conn = await get_connection(session, connection_id)
    if conn.status == "disconnected":
        return None
    gs = await load_settings(session, connection_id)
    if gs is None:
        return None
    label = await read_external_id(session, conn, tenant_id)
    projects = [
        r.provider_account_id
        for r in await session.execute(SELECT_PROJECTS, {"c": connection_id, "n": PROJECTS_CHECKED})
    ]
    result = await check_access(GcpApi(), gs, label, projects, date.today())

    try:
        async with session.begin_nested():
            stored = await _store(session, connection_id, result, result.verified)
    except IntegrityError:
        result = GcpValidation(
            "failed",
            [
                Check(
                    "ownership",
                    "Prove the dataset is yours",
                    "error",
                    "This billing account is already connected to another Cloudoptics workspace.",
                    "Disconnect it there first, or ask that workspace's admin to invite you.",
                )
            ],
            error={"code": "AccountInUse", "check": "ownership"},
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


def service_account() -> str:
    return get_gcp_settings().service_account or "cloudoptics-reader@<not-configured>"
