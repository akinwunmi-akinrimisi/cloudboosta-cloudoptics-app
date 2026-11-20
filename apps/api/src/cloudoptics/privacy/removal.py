"""What to remove in each provider when a workspace is deleted (PRD US-8.3, ADM-7).

Cloudoptics only reads, so it cannot undo what the customer set up. The workspace-deletion
emails and the Settings page list, for each connection the workspace had, what the customer
removes there, in the names and commands of that provider's own set-up steps:

- AWS: the CloudFormation stacks (``service.stack_steps``, unchanged).
- Google Cloud (ADR-019): the IAM bindings given to the Cloudoptics service account (BigQuery
  Data Viewer on the dataset, BigQuery Job User on the query project, the optional project
  roles) and the ``cloudoptics`` dataset label (``providers/gcp/connect.commands``).
- Azure (ADR-020): the role assignments of the Cloudoptics app, the ``cloudoptics-verify``
  tag on the export's storage account, then the app's enterprise application (service
  principal) in the directory (``providers/azure/connection.role_command``).
- DigitalOcean (ADR-021): the read-only token, revoked in DigitalOcean (Cloudoptics deletes
  its own copy).

Disconnected connections are listed too: disconnecting stops Cloudoptics reading, but what
the customer granted stays until they remove it.
"""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.azure import connection as azconn
from cloudoptics.providers.gcp import connect as gcp
from cloudoptics.providers.naming import connection_label

DO_TOKENS_URL = "https://cloud.digitalocean.com/account/api/tokens"

GCP_CONNECTIONS = text(
    "SELECT c.id, c.provider_account_id, c.account_type, g.export_project_id, "
    "g.export_dataset_id, g.export_kind, g.query_project_id, g.dataset_location "
    "FROM cloud_connection c JOIN gcp_connection g ON g.connection_id = c.id "
    "WHERE c.provider = 'gcp' ORDER BY c.provider_account_id"
)
GCP_PROJECTS = text(
    "SELECT provider_account_id FROM cloud_account WHERE connection_id = :c AND provider = 'gcp' "
    "AND account_type = 'member' ORDER BY provider_account_id LIMIT 50"
)
AZURE_CONNECTIONS = text(
    "SELECT id FROM cloud_connection WHERE provider = 'azure' ORDER BY provider_account_id"
)
DO_CONNECTIONS = text(
    "SELECT provider_account_id, account_type FROM cloud_connection "
    "WHERE provider = 'digitalocean' ORDER BY provider_account_id"
)


@dataclass(frozen=True)
class Removal:
    """One connection's clean-up: plain steps, then commands to paste (may be empty)."""

    provider: str
    label: str
    steps: list[str]
    commands: list[str] = field(default_factory=list)


def _q(value: str) -> str:
    return shlex.quote(value)


def gcp_removal(
    account_id: str,
    *,
    export_project_id: str,
    export_dataset_id: str,
    export_kind: str,
    query_project_id: str,
    dataset_location: str | None,
    projects: list[str],
    service_account: str,
) -> Removal:
    """The set-up commands of ``gcp.commands``, undone in reverse."""
    member = f"serviceAccount:{service_account}"
    dataset = f"{export_project_id}:{export_dataset_id}"
    location = f" --location={_q(dataset_location)}" if dataset_location else ""
    revoke = (
        f"REVOKE `roles/bigquery.dataViewer` ON SCHEMA "
        f'`{export_project_id}.{export_dataset_id}` FROM "{member}"'
    )
    scanned = " ".join(_q(p) for p in (projects or [export_project_id]))
    roles = " ".join(gcp.PROJECT_ROLES)
    steps = [
        f"Remove the IAM bindings you gave the Cloudoptics service account {service_account} "
        f"(BigQuery Data Viewer on the dataset {export_project_id}.{export_dataset_id}, "
        f"BigQuery Job User on the project {query_project_id}, and the optional project roles "
        "for resources), and the dataset label "
        f"{gcp.LABEL_KEY}. Run these in Cloud Shell; a binding you never added says it is "
        "not found, which is fine.",
    ]
    if export_kind == "focus_view":
        steps.append(
            f"The export is a view: remove the {gcp.LABEL_KEY} label from each dataset the view "
            "reads too."
        )
    return Removal(
        provider="gcp",
        label=connection_label("gcp", account_id),
        steps=steps,
        commands=[
            f"bq query --nouse_legacy_sql{location} {_q(revoke)}",
            f"gcloud projects remove-iam-policy-binding {_q(query_project_id)} "
            f"--member={_q(member)} --role=roles/bigquery.jobUser --condition=None",
            f"for p in {scanned}; do for r in {roles}; do gcloud projects "
            f'remove-iam-policy-binding "$p" --member={_q(member)} --role="$r" '
            "--condition=None; done; done",
            f"bq update --clear_label {gcp.LABEL_KEY} {_q(dataset)}",
        ],
    )


def azure_removal(s: azconn.AzureSettings, account_type: str, code: str | None) -> Removal:
    """The set-up script of ``azconn.role_command``, undone: role assignments first, then the
    tag, then the app's service principal (deleting it first would leave the assignments
    behind as "Identity not found")."""
    app = get_settings().azure_client_id or "<cloudoptics-app-id>"
    app_name = azconn.APP_NAME
    commands = [f"APP_ID={_q(app)}"]
    if s.scope_type == "subscription":
        commands.append(f'az role assignment delete --assignee "$APP_ID" --scope {_q(s.scope)}')
    else:
        commands += [
            f'az role assignment delete --assignee "$APP_ID" --scope {_q("/subscriptions/" + sub)}'
            for sub in s.subscription_ids
        ]
    commands += [
        f"STORAGE_ID=$(az storage account list --query \"[?name=='{s.storage_account}'].id\" "
        "-o tsv)",
        'az role assignment delete --assignee "$APP_ID" --scope '
        f'"$STORAGE_ID/blobServices/default/containers/{s.container}"',
        'az role assignment delete --assignee "$APP_ID" --scope "$STORAGE_ID"',
    ]
    if code:
        commands.append(
            f'az tag update --resource-id "$STORAGE_ID" --operation Delete --tags '
            f"{_q(f'{azconn.OWNERSHIP_TAG}={code}')}"
        )
    commands.append('az ad sp delete --id "$APP_ID"')
    steps = [
        f"In directory {s.directory_id}, as an Owner or User Access Administrator, remove the "
        f"role assignments of the {app_name} app (on the scope and on the storage account "
        f"{s.storage_account} and its container {s.container}).",
    ]
    if s.scope_type == "billing_account":
        steps.append(
            f"Remove the billing role you gave {app_name} on the billing account "
            f"{s.billing_account_id}: for an Enterprise Agreement the EnrollmentReader role "
            "(through the billing role assignments API), for a Microsoft Customer Agreement "
            "the billing account Reader role in Cost Management + Billing, Access control (IAM)."
        )
    tag_command = ", or the az tag command below" if code else ""
    steps += [
        f"Remove the tag {azconn.OWNERSHIP_TAG} from the storage account {s.storage_account} "
        f"(Tags in the portal{tag_command}).",
        f"Last, delete the {app_name} enterprise application (its service principal) from the "
        "directory: Microsoft Entra ID, Enterprise applications, "
        f"{app_name}, Properties, Delete (or az ad sp delete below). This also withdraws the "
        "admin consent. Skip it if another Cloudoptics workspace still reads this directory.",
    ]
    return Removal(
        provider="azure",
        label=connection_label("azure", s.account_id, account_type),
        steps=steps,
        commands=commands,
    )


def digitalocean_removal(team: str, account_type: str | None = None) -> Removal:
    return Removal(
        provider="digitalocean",
        label=connection_label("digitalocean", team, account_type),
        steps=[
            "Cloudoptics deletes its copy of the token. Revoke the token itself in "
            f"DigitalOcean: API, Tokens ({DO_TOKENS_URL}), then Delete next to the read-only "
            "token you made for Cloudoptics.",
        ],
    )


async def removal_steps(session: AsyncSession, tenant_id: uuid.UUID) -> list[Removal]:
    """Every non-AWS connection's clean-up, Google Cloud, Azure, then DigitalOcean (tenant
    scope already set; before the data is deleted)."""
    out: list[Removal] = []
    sa = gcp.service_account()
    for r in (await session.execute(GCP_CONNECTIONS)).all():
        projects = [str(p) for p in (await session.execute(GCP_PROJECTS, {"c": r.id})).scalars()]
        out.append(
            gcp_removal(
                str(r.provider_account_id).strip(),
                export_project_id=r.export_project_id,
                export_dataset_id=r.export_dataset_id,
                export_kind=r.export_kind,
                query_project_id=r.query_project_id,
                dataset_location=r.dataset_location,
                projects=projects,
                service_account=sa,
            )
        )
    for (connection_id,) in (await session.execute(AZURE_CONNECTIONS)).all():
        row = await azconn.load(session, connection_id)
        if row is None:
            continue
        code = (
            get_encryptor().decrypt(bytes(row.external_id_enc), tenant_id)
            if row.external_id_enc is not None
            else None
        )
        out.append(azure_removal(azconn.from_row(row), row.account_type, code))
    for r in (await session.execute(DO_CONNECTIONS)).all():
        out.append(digitalocean_removal(str(r.provider_account_id).strip(), r.account_type))
    return out


def removal_lines(removals: list[Removal]) -> list[str]:
    """Email lines: a heading per connection, its steps, then each command as a list item."""
    if not removals:
        return []
    lines = [
        "Cloudoptics cannot remove what you set up in your other clouds either. For each "
        "connection below:",
    ]
    for r in removals:
        lines.append("")
        lines.append(f"{r.label}:")
        lines += [f"- {step}" for step in r.steps]
        if r.commands:
            lines.append("Commands:")
            lines += [f"- {c}" for c in r.commands]
    return lines
