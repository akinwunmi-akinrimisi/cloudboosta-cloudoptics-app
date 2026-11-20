"""/v1/tenants/{t}/connections/gcp: create a Google Cloud connection and show its setup
(ADR-019). Checking it, listing, reloading bills and disconnecting use the shared
connection routes, which dispatch to the plug-in by provider (ADR-018)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cloudoptics.audit import log as audit
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.routes import Connection, _to_model
from cloudoptics.connectors.service import ConnectionNotFoundError, get_connection, read_external_id
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.gcp import connect
from cloudoptics.providers.gcp.settings import get_gcp_settings

router = APIRouter(prefix="/v1/tenants/{tenant_id}/connections/gcp", tags=["connections"])

PROJECT = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"

EXISTING = text(
    "SELECT id, status FROM cloud_connection WHERE provider = 'gcp' AND provider_account_id = :a"
)
INSERT_CONNECTION = text(
    "INSERT INTO cloud_connection (id, tenant_id, provider, provider_account_id, account_type, "
    "role_arn, external_id_enc, stack_name, create_cur_export, created_by) VALUES (:id, :t, "
    "'gcp', :a, 'management', :sa, :ext, :stack, false, :by)"
)
RECONNECT = text(
    "UPDATE cloud_connection SET status = 'pending', health = NULL, role_arn = :sa, "
    "external_id_enc = :ext, permission_checks = '[]', last_error = NULL, "
    "last_validated_at = NULL, account_verified_at = NULL, created_by = :by WHERE id = :id"
)
UPSERT_SETTINGS = text(
    "INSERT INTO gcp_connection (connection_id, tenant_id, billing_account_id, "
    "export_project_id, export_dataset_id, export_table_id, export_kind, query_project_id, "
    "dataset_location) VALUES (:c, :t, :ba, :p, :d, :tbl, :kind, :q, :loc) "
    "ON CONFLICT (connection_id) DO UPDATE SET export_project_id = EXCLUDED.export_project_id, "
    "export_dataset_id = EXCLUDED.export_dataset_id, export_table_id = EXCLUDED.export_table_id, "
    "export_kind = EXCLUDED.export_kind, query_project_id = EXCLUDED.query_project_id, "
    "dataset_location = EXCLUDED.dataset_location, updated_at = now()"
)
KNOWN_PROJECTS = text(
    "SELECT provider_account_id FROM cloud_account WHERE connection_id = :c AND provider = 'gcp' "
    "AND account_type = 'member' ORDER BY provider_account_id LIMIT 50"
)

ExportKind = Literal["focus_export", "focus_view"]


class GcpConnectionCreate(ApiModel):
    billing_account_id: str = Field(pattern=r"^[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}$")
    export_project_id: str = Field(pattern=PROJECT)
    export_dataset_id: str = Field(pattern=r"^[A-Za-z0-9_]{1,1024}$")
    export_table_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,1024}$")
    export_kind: ExportKind = "focus_export"
    query_project_id: str | None = Field(default=None, pattern=PROJECT)
    dataset_location: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{2,40}$")


class GcpSettingsOut(ApiModel):
    billing_account_id: str
    export_project_id: str
    export_dataset_id: str
    export_table_id: str
    export_kind: ExportKind
    query_project_id: str
    dataset_location: str | None


class GcpCommand(ApiModel):
    id: str
    title: str
    command: str
    optional: bool


class GcpSetup(ApiModel):
    connection: Connection
    settings: GcpSettingsOut
    service_account: str
    label_key: str = connect.LABEL_KEY
    label_value: str
    commands: list[GcpCommand]
    console_steps: list[str]
    #: False until Cloudoptics' own Google Cloud identity is set up (owner steps, ADR-019).
    enabled: bool


async def _setup(session: Session, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> GcpSetup:
    try:
        row = await get_connection(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from exc
    gs = await connect.load_settings(session, connection_id)
    if row.provider != "gcp" or gs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    label = await read_external_id(session, row, tenant_id)
    projects = [r.provider_account_id for r in await session.execute(KNOWN_PROJECTS, {"c": row.id})]
    sa = connect.service_account()
    return GcpSetup(
        connection=_to_model(row),
        settings=GcpSettingsOut.model_validate(gs),
        service_account=sa,
        label_value=label,
        commands=[
            GcpCommand.model_validate(c)
            for c in connect.commands(gs, service_account=sa, label=label, projects=projects)
        ],
        console_steps=connect.console_steps(gs, service_account=sa, label=label),
        enabled=get_gcp_settings().enabled,
    )


@router.post("", response_model=GcpSetup, status_code=status.HTTP_201_CREATED)
async def create_gcp_connection(
    body: GcpConnectionCreate, request: Request, access: AdminAccess, session: Session
) -> GcpSetup:
    billing_account = body.billing_account_id.upper()
    label = connect.new_label_value()
    sealed = get_encryptor().encrypt(label, access.tenant_id)
    sa = connect.service_account()
    existing = (await session.execute(EXISTING, {"a": billing_account})).one_or_none()
    if existing is not None and existing.status != "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This billing account is already connected to this workspace. Use Check now or "
            "Disconnect on the Connections page.",
        )
    if existing is not None:
        connection_id = existing.id
        await session.execute(
            RECONNECT, {"id": connection_id, "sa": sa, "ext": sealed, "by": access.user.id}
        )
        action, before = "connection.reconnected", {"status": "disconnected"}
    else:
        connection_id = uuid7()
        try:
            async with session.begin_nested():
                await session.execute(
                    INSERT_CONNECTION,
                    {
                        "id": connection_id,
                        "t": access.tenant_id,
                        "a": billing_account,
                        "sa": sa,
                        "ext": sealed,
                        "stack": connect.STACK_NAME,
                        "by": access.user.id,
                    },
                )
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This billing account is already connected to this workspace.",
            ) from exc
        action, before = "connection.created", None
    await session.execute(
        UPSERT_SETTINGS,
        {
            "c": connection_id,
            "t": access.tenant_id,
            "ba": billing_account,
            "p": body.export_project_id,
            "d": body.export_dataset_id,
            "tbl": body.export_table_id,
            "kind": body.export_kind,
            "q": body.query_project_id or body.export_project_id,
            "loc": body.dataset_location,
        },
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action=action,
        target_type="cloud_connection",
        target_id=str(connection_id),
        before=before,
        after={
            "provider": "gcp",
            "billingAccountId": billing_account,
            "dataset": f"{body.export_project_id}:{body.export_dataset_id}",
            "table": body.export_table_id,
        },
    )
    return await _setup(session, access.tenant_id, connection_id)


@router.get("/{connection_id}", response_model=GcpSetup)
async def get_gcp_setup(
    connection_id: uuid.UUID, access: AdminAccess, session: Session
) -> GcpSetup:
    return await _setup(session, access.tenant_id, connection_id)
