"""Validation of an Azure connection: one check per permission (``permission_checks``).

Checks, in order (ids as the web app shows them):

- ``consent``: a token in the customer's directory (admin consent given). Failing it fails
  the connection; nothing else is tried.
- ``reader``: Reader on the subscription (or the billing account's reader role, and Reader on
  each listed subscription).
- ``cost_reader``: Cost Management Reader: the scope's exports can be listed.
- ``export``: a FOCUS export of the scope writes to the storage account and container given.
- ``ownership``: the export's storage account carries this connection's code in the
  ``cloudoptics-verify`` tag. Until it does the connection stays ``failed``: Cloudoptics
  reads nothing it cannot prove belongs to this workspace (connection.py).
- ``blob_reader``: Storage Blob Data Reader on the container.
- ``export_data``: the export has delivered at least once (a warning until it has).

``failed`` when consent or ownership is missing, ``degraded`` when another check errors,
otherwise ``healthy``. Azure answers these calls for free.
"""

from __future__ import annotations

import hmac
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analytics import funnel
from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.notifications.alerts import should_alert
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.azure import connection as azconn
from cloudoptics.providers.azure.client import API, AzureApi, AzureError
from cloudoptics.providers.azure.credentials import AzureAuthError

CheckStatus = Literal["ok", "warning", "error", "skipped"]
EXPORTS_API = API["cost_management"]
SUBSCRIPTION_API = API["subscriptions"]
BILLING_API = API["billing"]
STORAGE_API = API["storage"]

LABELS = {
    "consent": "Admin consent for Cloudoptics",
    "reader": "Read the subscription",
    "cost_reader": "Read Cost Management",
    "export": "FOCUS cost export",
    "ownership": "Prove the storage account is yours",
    "blob_reader": "Read the export container",
    "export_data": "First export delivered",
}


@dataclass
class Check:
    id: str
    label: str
    status: CheckStatus
    message: str
    hint: str | None = None


@dataclass
class AzureValidation:
    status: Literal["healthy", "degraded", "failed"]
    checks: list[Check]
    error: dict[str, str] | None = None
    storage_account_id: str | None = None
    export_name: str | None = None
    verified: bool = False
    health_worsened: bool = False
    exported: bool = False

    @property
    def has_billing_export(self) -> bool:
        return self.exported and self.status != "failed"

    def checks_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.checks]


def _check(check_id: str, status: CheckStatus, message: str, hint: str | None = None) -> Check:
    return Check(check_id, LABELS[check_id], status, message, hint)


def _norm(path: str | None) -> str:
    return (path or "").strip("/").lower()


def matching_export(
    exports: list[dict[str, Any]], s: azconn.AzureSettings
) -> tuple[dict[str, Any] | None, str | None]:
    """The scope's FOCUS export into this container, and a note when its folder differs."""
    for export in exports:
        props = export.get("properties") or {}
        kind = str((props.get("definition") or {}).get("type") or "")
        dest = (props.get("deliveryInfo") or {}).get("destination") or {}
        resource_id = str(dest.get("resourceId") or "")
        if kind.lower() != "focuscost":
            continue
        if not resource_id.lower().endswith(f"/storageaccounts/{s.storage_account}"):
            continue
        if str(dest.get("container") or "").lower() != s.container:
            continue
        note = None
        if _norm(dest.get("rootFolderPath")) != _norm(s.export_path):
            note = str(dest.get("rootFolderPath") or "")
        return export, note
    return None, None


async def run_checks(api: AzureApi, s: azconn.AzureSettings, code: str) -> AzureValidation:
    checks: list[Check] = []
    try:
        await api.sign_in()
    except AzureAuthError as exc:
        hint = {
            "consent": "Ask a Global Administrator to open the admin consent link, then check "
            "again.",
            "directory": "Check the Directory (tenant) ID: Entra ID, Overview, Tenant ID.",
            "credential": "Cloudoptics' Azure credential is not working; we have been told.",
        }.get(exc.kind, "Try again in a few minutes.")
        message = {
            "consent": "Cloudoptics is not approved in this directory yet.",
            "directory": "No Entra directory has this ID.",
            "credential": "Cloudoptics could not sign in to Azure.",
        }.get(exc.kind, f"Entra ID refused the sign-in ({exc.code}).")
        checks.append(_check("consent", "error", message, hint))
        return AzureValidation("failed", checks, error={"code": exc.code, "check": "consent"})
    checks.append(_check("consent", "ok", "Cloudoptics is approved in your directory."))

    # Reader
    try:
        if s.scope_type == "subscription":
            await api.arm("GET", s.scope, api_version=SUBSCRIPTION_API)
        else:
            await api.arm("GET", s.scope, api_version=BILLING_API)
        for sub in s.subscription_ids if s.scope_type == "billing_account" else ():
            await api.arm("GET", f"/subscriptions/{sub}", api_version=SUBSCRIPTION_API)
        checks.append(_check("reader", "ok", "Cloudoptics can read the scope."))
    except AzureError as exc:
        role = (
            "the Reader role"
            if s.scope_type == "subscription"
            else "read access to the billing account (EA: EnrollmentReader; MCA: Reader)"
        )
        checks.append(
            _check(
                "reader",
                "error",
                f"Cloudoptics cannot read {s.scope} ({exc.code}).",
                f"Give Cloudoptics {role} (step 2).",
            )
        )

    exports: list[dict[str, Any]] | None = None
    try:
        exports = await api.arm_list(
            f"{s.scope}/providers/Microsoft.CostManagement/exports", api_version=EXPORTS_API
        )
        checks.append(_check("cost_reader", "ok", "Cloudoptics can read Cost Management."))
    except AzureError as exc:
        checks.append(
            _check(
                "cost_reader",
                "error",
                f"Cloudoptics cannot read Cost Management for this scope ({exc.code}).",
                "Give Cloudoptics the Cost Management Reader role (step 2).",
            )
        )

    result = AzureValidation("healthy", checks)
    export = None
    if exports is None:
        checks.append(_check("export", "skipped", "Needs Cost Management access first."))
    else:
        export, other_folder = matching_export(exports, s)
        if export is None:
            checks.append(
                _check(
                    "export",
                    "error",
                    f"No FOCUS export of this scope writes to {s.storage_account}/{s.container}.",
                    "Create the Cost and usage (FOCUS) export (step 3).",
                )
            )
        else:
            dest = export["properties"]["deliveryInfo"]["destination"]
            result.storage_account_id = str(dest["resourceId"])
            result.export_name = str(export.get("name") or "")
            if other_folder is not None:
                checks.append(
                    _check(
                        "export",
                        "warning",
                        f"Export {result.export_name} writes to the folder '{other_folder}', not "
                        f"'{s.export_path}'.",
                        "Change the folder here or in the export so they match.",
                    )
                )
            else:
                checks.append(
                    _check("export", "ok", f"Export {result.export_name} writes to your container.")
                )

    # Ownership: the tag on the storage account the scope's own export writes to.
    if result.storage_account_id is None:
        checks.append(_check("ownership", "skipped", "Needs the FOCUS export first."))
    else:
        try:
            account = await api.arm("GET", result.storage_account_id, api_version=STORAGE_API)
            tags = {str(k).lower(): str(v) for k, v in (account.get("tags") or {}).items()}
            found = tags.get(azconn.OWNERSHIP_TAG, "")
            if hmac.compare_digest(found.encode(), code.encode()):
                result.verified = True
                checks.append(_check("ownership", "ok", "The storage account carries your code."))
            else:
                checks.append(
                    _check(
                        "ownership",
                        "error",
                        f"The storage account does not have the {azconn.OWNERSHIP_TAG} tag with "
                        "this connection's code.",
                        "Run the commands in step 2 again, or add the tag in the portal.",
                    )
                )
        except AzureError as exc:
            checks.append(
                _check(
                    "ownership",
                    "error",
                    f"Cloudoptics cannot read the storage account ({exc.code}).",
                    "Give Cloudoptics the Reader role on the storage account (step 2).",
                )
            )

    prefix = f"{s.export_path.strip('/')}/" if s.export_path.strip("/") else ""
    try:
        blobs = await api.list_blobs(s.storage_account, s.container, prefix)
        checks.append(_check("blob_reader", "ok", "Cloudoptics can read the export container."))
        if any(b.name.endswith("manifest.json") for b in blobs):
            result.exported = True
            checks.append(_check("export_data", "ok", "The export has delivered cost data."))
        else:
            checks.append(
                _check(
                    "export_data",
                    "warning",
                    "No export file yet.",
                    "In Exports, choose Run now; the first file can take a few hours.",
                )
            )
    except AzureError as exc:
        checks.append(
            _check(
                "blob_reader",
                "error",
                f"Cloudoptics cannot list the container {s.container} ({exc.code}).",
                "Give Cloudoptics the Storage Blob Data Reader role on the container (step 2).",
            )
        )
        checks.append(_check("export_data", "skipped", "Needs access to the container first."))

    if not result.verified:
        result.status = "failed"
        result.error = {"code": "NotVerified", "check": "ownership"}
    elif any(c.status == "error" for c in checks):
        result.status = "degraded"
        first = next(c for c in checks if c.status == "error")
        result.error = {"code": "CheckFailed", "check": first.id}
    return result


async def validate(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
) -> AzureValidation | None:
    """Validate one Azure connection inside the caller's transaction (tenant scope set).
    None for a disconnected (or unknown) connection."""
    row = await azconn.load(session, connection_id)
    if row is None or row.status == "disconnected":
        return None
    s = azconn.from_row(row)
    code = get_encryptor().decrypt(bytes(row.external_id_enc), tenant_id)
    started = datetime.now(UTC)
    api = AzureApi(s.directory_id)
    result = await run_checks(api, s, code)
    try:
        async with session.begin_nested():
            stored = await _store(session, s, result)
    except IntegrityError:
        result = AzureValidation(
            "failed",
            [
                *[c for c in result.checks if c.id != "ownership"],
                _check(
                    "ownership",
                    "error",
                    "This Azure scope is already connected to another Cloudoptics workspace.",
                    "Disconnect it there first, or ask that workspace's admin to invite you.",
                ),
            ],
            error={"code": "AccountInUse", "check": "ownership"},
        )
        stored = await _store(session, s, result)
    if not stored:
        return None
    if result.status != "failed":
        await funnel.emit(session, "connected", tenant_id=tenant_id)
        await _accounts(session, s)
    await session.execute(
        text(
            "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
            "finished_at, error_code, est_customer_api_cost_usd) VALUES (:id, :t, :c, "
            "'connection.validate', :status, :started, now(), :code, 0)"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": connection_id,
            "status": "failed" if result.status == "failed" else "succeeded",
            "started": started,
            "code": (result.error or {}).get("code"),
        },
    )
    result.health_worsened = should_alert(row.health, result.status)
    if result.status != row.status:
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="connection.status_changed",
            target_type="cloud_connection",
            target_id=str(connection_id),
            before={"status": row.status},
            after={"status": result.status},
        )
    return result


async def _store(session: AsyncSession, s: azconn.AzureSettings, result: AzureValidation) -> bool:
    stored = await session.execute(
        text(
            "UPDATE cloud_connection SET status = :status, health = :status, "
            "permission_checks = CAST(:checks AS jsonb), last_error = CAST(:err AS jsonb), "
            "last_validated_at = now(), account_verified_at = CASE WHEN :verified THEN "
            "coalesce(account_verified_at, now()) ELSE NULL END "
            "WHERE id = :c AND status <> 'disconnected'"
        ),
        {
            "status": result.status,
            "checks": json.dumps(result.checks_json()),
            "err": json.dumps(result.error) if result.error else None,
            "verified": result.verified,
            "c": s.id,
        },
    )
    if not getattr(stored, "rowcount", 0):
        return False
    await session.execute(
        text(
            "UPDATE azure_connection SET storage_account_id = coalesce(:sid, storage_account_id), "
            "export_name = coalesce(:name, export_name) WHERE connection_id = :c"
        ),
        {"sid": result.storage_account_id, "name": result.export_name, "c": s.id},
    )
    return True


async def _accounts(session: AsyncSession, s: azconn.AzureSettings) -> None:
    """The connection's subscriptions as accounts (named later, from the bill)."""
    subs = list(s.subscription_ids) or ([s.account_id] if s.scope_type == "subscription" else [])
    for sub in subs:
        await session.execute(
            text(
                "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, "
                "provider_account_id, account_type, resource_access) VALUES (:id, :t, :c, "
                "'azure', :acct, :type, true) ON CONFLICT (tenant_id, provider, "
                "provider_account_id) DO UPDATE SET resource_access = true"
            ),
            {
                "id": uuid7(),
                "t": s.tenant_id,
                "c": s.id,
                "acct": sub.lower(),
                "type": "standalone" if s.scope_type == "subscription" else "member",
            },
        )
