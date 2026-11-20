"""An Azure connection's settings and the customer's set-up steps (ADR-020).

The customer does three things in Azure, all read-only for Cloudoptics:

1. **Admin consent** to the multi-tenant "Cloudoptics" app in their directory (creates its
   service principal; the app asks for no Microsoft Graph permission).
2. **Roles** for that service principal: Reader and Cost Management Reader on the scope (a
   billing role for a billing account), Storage Blob Data Reader on the export container,
   and the **ownership tag** ``cloudoptics-verify=<code>`` on the export's storage account.
3. A **Cost Management FOCUS export** (daily, month to date) into that container.

Why the tag: every customer trusts the same app, so the app's access alone does not prove
that the workspace asking is the customer's. AWS uses a per-connection External ID for this;
here the per-connection code must appear on the storage account the scope's own export
writes to, which only someone with write access to the customer's Azure can arrange. Until
it does, the connection is not verified and nothing is read beyond the checks.
"""

from __future__ import annotations

import re
import secrets
import shlex
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.platform.settings import get_settings

ScopeType = Literal["subscription", "billing_account"]
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
OWNERSHIP_TAG = "cloudoptics-verify"
APP_NAME = "Cloudoptics"
EXPORTS_PORTAL_URL = "https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/exports"
STACK_NAME = "azure"
EA_SPN_DOCS = "https://learn.microsoft.com/azure/cost-management-billing/manage/assign-roles-azure-service-principals"

SELECT_AZURE = text(
    "SELECT c.id, c.tenant_id, c.provider, c.provider_account_id, c.account_type, c.status, "
    "c.health, c.permission_checks, c.last_error, c.last_validated_at, c.created_at, "
    "c.external_id_enc, c.account_verified_at, a.directory_id, a.scope_type, "
    "a.billing_account_id, a.subscription_ids, a.storage_account, a.container, a.export_path, "
    "a.storage_account_id, a.export_name FROM cloud_connection c "
    "JOIN azure_connection a ON a.connection_id = c.id WHERE c.id = :c"
)


@dataclass(frozen=True)
class AzureSettings:
    """What the Azure code needs of one connection."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    directory_id: str
    scope_type: ScopeType
    billing_account_id: str | None
    subscription_ids: tuple[str, ...]
    storage_account: str
    container: str
    export_path: str
    storage_account_id: str | None
    export_name: str | None
    verified: bool
    account_id: str

    @property
    def scope(self) -> str:
        return arm_scope(self.scope_type, self.subscription_ids, self.billing_account_id)


def arm_scope(
    scope_type: str, subscription_ids: tuple[str, ...] | list[str], billing_account_id: str | None
) -> str:
    if scope_type == "billing_account":
        return f"/providers/Microsoft.Billing/billingAccounts/{billing_account_id}"
    return f"/subscriptions/{subscription_ids[0]}"


def from_row(row: Any) -> AzureSettings:
    return AzureSettings(
        id=row.id,
        tenant_id=row.tenant_id,
        status=row.status,
        directory_id=str(row.directory_id),
        scope_type=row.scope_type,
        billing_account_id=row.billing_account_id,
        subscription_ids=tuple(row.subscription_ids or ()),
        storage_account=row.storage_account,
        container=row.container,
        export_path=row.export_path or "",
        storage_account_id=row.storage_account_id,
        export_name=row.export_name,
        verified=row.account_verified_at is not None,
        account_id=row.provider_account_id,
    )


async def load(session: AsyncSession, connection_id: uuid.UUID) -> Any | None:
    """The joined row (tenant scope already set), or None."""
    return (await session.execute(SELECT_AZURE, {"c": connection_id})).one_or_none()


def new_ownership_code() -> str:
    return f"co-{secrets.token_hex(12)}"


def consent_url(directory_id: str, connection_id: uuid.UUID) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.azure_client_id,
            "redirect_uri": f"{settings.public_api_url}/v1/azure/consent",
            "state": str(connection_id),
        }
    )
    return f"{settings.azure_authority_url.rstrip('/')}/{directory_id}/adminconsent?{query}"


def _q(value: str) -> str:
    return shlex.quote(value)


def role_command(s: AzureSettings, code: str) -> str:
    """The Cloud Shell (Bash) script that gives the app its roles and sets the ownership tag."""
    app = get_settings().azure_client_id or "<cloudoptics-app-id>"
    lines = [
        "# Azure Cloud Shell (Bash), as an Owner or User Access Administrator.",
        f"APP_ID={_q(app)}",
    ]
    if s.scope_type == "subscription":
        lines += [
            f"SCOPE={_q(s.scope)}",
            'az role assignment create --assignee "$APP_ID" --role "Reader" --scope "$SCOPE"',
            'az role assignment create --assignee "$APP_ID" --role "Cost Management Reader" '
            '--scope "$SCOPE"',
        ]
    else:
        lines.append(
            "# Billing account: give the app read access to costs there first (EA: the "
            "EnrollmentReader role, by API; MCA: the billing account Reader role). See the steps."
        )
        for sub in s.subscription_ids:
            lines.append(
                f'az role assignment create --assignee "$APP_ID" --role "Reader" '
                f"--scope {_q('/subscriptions/' + sub)}"
            )
    lines += [
        # JMESPath raw-string literal; the name is checked to be [a-z0-9]{3,24}.
        f"STORAGE_ID=$(az storage account list --query \"[?name=='{s.storage_account}'].id\" "
        "-o tsv)",
        'az role assignment create --assignee "$APP_ID" --role "Reader" --scope "$STORAGE_ID"',
        'az role assignment create --assignee "$APP_ID" --role "Storage Blob Data Reader" '
        f'--scope "$STORAGE_ID/blobServices/default/containers/{s.container}"',
        f'az tag update --resource-id "$STORAGE_ID" --operation Merge --tags '
        f"{_q(f'{OWNERSHIP_TAG}={code}')}",
    ]
    return "\n".join(lines)


def portal_role_steps(s: AzureSettings, code: str) -> list[str]:
    steps = []
    if s.scope_type == "subscription":
        steps.append(
            f"Open the subscription {s.subscription_ids[0]}, then Access control (IAM), Add role "
            f"assignment. Give {APP_NAME} the Reader role, then again the Cost Management "
            "Reader role."
        )
    else:
        steps.append(
            f"Give {APP_NAME} read access to costs on the billing account "
            f"{s.billing_account_id}: for an Enterprise Agreement, the EnrollmentReader role "
            "(role definition 24f8edb6-1668-4659-b5e2-40bb5f3a7d7e, given to an app only through "
            f"the billing role assignments API: {EA_SPN_DOCS}); for a Microsoft Customer "
            "Agreement, the billing account Reader role in Cost Management + Billing, Access "
            "control (IAM)."
        )
        if s.subscription_ids:
            steps.append(
                f"On each subscription to scan for resources, give {APP_NAME} the Reader role."
            )
    steps += [
        f"Open the storage account {s.storage_account}, Access control (IAM): give {APP_NAME} the "
        "Reader role.",
        f"Open its container {s.container}, Access control (IAM): give {APP_NAME} the Storage "
        "Blob Data Reader role.",
        f"On the storage account, open Tags and add the tag {OWNERSHIP_TAG} with the value "
        f"{code}. It proves the account is yours.",
    ]
    return steps


def export_steps(s: AzureSettings) -> list[str]:
    folder = s.export_path or "(leave empty)"
    return [
        f"In Cost Management, choose the scope {s.scope} and open Exports.",
        "Create an export of type Cost and usage (FOCUS), named for example cloudoptics-focus.",
        "Choose Daily export of month-to-date costs.",
        f"Storage: account {s.storage_account}, container {s.container}, directory {folder}. "
        "Format Parquet or CSV; turn on Overwrite data.",
        "Create it, then choose Run now so the first file arrives straight away.",
    ]
