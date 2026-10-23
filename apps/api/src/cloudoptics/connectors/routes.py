"""/v1/tenants/{t}/connections (docs/09 §3.2 "Cloud accounts & connections")."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from procrastinate.exceptions import AlreadyEnqueued
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cloudoptics.audit import log as audit
from cloudoptics.connectors import accounts, aws
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import (
    LIST_CONNECTIONS,
    ConnectionNotFoundError,
    get_connection,
    run_validation,
)
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/tenants/{tenant_id}/connections", tags=["connections"])
accounts_router = APIRouter(prefix="/v1/tenants/{tenant_id}/accounts", tags=["connections"])

AccountType = Literal["management", "standalone"]


class ConnectionCreate(ApiModel):
    aws_account_id: str = Field(pattern=r"^\d{12}$")
    account_type: AccountType = "standalone"
    create_cur_export: bool = True
    existing_cur_bucket_name: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
    )


class PermissionCheck(ApiModel):
    id: str
    label: str
    status: Literal["ok", "warning", "error", "skipped"]
    message: str
    hint: str | None = None


class Connection(ApiModel):
    id: uuid.UUID
    aws_account_id: str
    account_type: AccountType
    role_arn: str
    status: str
    create_cur_export: bool
    cur_bucket: str | None
    cur_prefix: str | None
    cur_export_arn: str | None
    permission_checks: list[PermissionCheck]
    last_error: dict[str, Any] | None
    last_validated_at: datetime | None
    created_at: datetime


class ConnectionSetup(ApiModel):
    connection: Connection
    external_id: str
    quick_create_url: str | None
    template_url: str
    template_download_url: str
    resource_template_url: str
    resource_template_download_url: str
    stack_region: str = aws.STACK_REGION
    principal_arn: str


class ValidationAccepted(ApiModel):
    connection_id: uuid.UUID
    status: Literal["validating"] = "validating"


def _to_model(row: Any) -> Connection:
    return Connection.model_validate(row._mapping)


def _setup(row: Any, external_id: str) -> ConnectionSetup:
    settings = get_settings()
    return ConnectionSetup(
        connection=_to_model(row),
        external_id=external_id,
        quick_create_url=None
        if not settings.cfn_quick_create_enabled
        else aws.quick_create_url(
            settings.cfn_billing_template_url,
            external_id=external_id,
            principal_arn=settings.cloudoptics_principal_arn,
            create_cur_export=row.create_cur_export,
            existing_bucket=row.cur_bucket,
            stack_name=row.stack_name,
        ),
        template_url=settings.cfn_billing_template_url,
        template_download_url=f"{settings.cfn_template_download_base}/billing-role.yaml",
        resource_template_url=settings.cfn_resource_template_url,
        resource_template_download_url=f"{settings.cfn_template_download_base}/resource-role.yaml",
        principal_arn=settings.cloudoptics_principal_arn,
    )


async def _load(session: Session, connection_id: uuid.UUID) -> Any:
    try:
        return await get_connection(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from exc


@router.post("", response_model=ConnectionSetup, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate, request: Request, access: AdminAccess, session: Session
) -> ConnectionSetup:
    external_id = aws.new_external_id()
    connection_id = uuid7()
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO cloud_connection (id, tenant_id, aws_account_id, account_type, "
                    "role_arn, external_id_enc, stack_name, create_cur_export, cur_bucket, "
                    "created_by) VALUES (:id, :t, :acct, :type, :arn, :ext, :stack, :cur, "
                    ":bucket, :by)"
                ),
                {
                    "id": connection_id,
                    "t": access.tenant_id,
                    "acct": body.aws_account_id,
                    "type": body.account_type,
                    "arn": aws.billing_role_arn(body.aws_account_id),
                    "ext": get_encryptor().encrypt(external_id, access.tenant_id),
                    "stack": aws.STACK_NAME,
                    "cur": body.create_cur_export and not body.existing_cur_bucket_name,
                    "bucket": body.existing_cur_bucket_name,
                    "by": access.user.id,
                },
            )
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This AWS account is already connected to a Cloudoptics workspace.",
        ) from exc
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="connection.created",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"awsAccountId": body.aws_account_id, "accountType": body.account_type},
    )
    return _setup(await _load(session, connection_id), external_id)


@router.get("", response_model=list[Connection])
async def list_connections(access: ViewerAccess, session: Session) -> list[Connection]:
    rows = await session.execute(LIST_CONNECTIONS)
    return [_to_model(r) for r in rows]


@router.get("/{connection_id}", response_model=Connection)
async def get_connection_detail(
    connection_id: uuid.UUID, access: ViewerAccess, session: Session
) -> Connection:
    return _to_model(await _load(session, connection_id))


@router.get("/{connection_id}/setup", response_model=ConnectionSetup)
async def get_setup(
    connection_id: uuid.UUID, access: AdminAccess, session: Session
) -> ConnectionSetup:
    row = await _load(session, connection_id)
    return _setup(row, get_encryptor().decrypt(bytes(row.external_id_enc), access.tenant_id))


@router.post(
    "/{connection_id}/validate",
    response_model=Connection | ValidationAccepted,
    responses={202: {"model": ValidationAccepted}},
)
async def validate(
    connection_id: uuid.UUID,
    request: Request,
    response: Response,
    access: AdminAccess,
    session: Session,
) -> Connection | ValidationAccepted:
    await _load(session, connection_id)
    if get_settings().jobs_mode == "inline":
        await run_validation(
            session,
            tenant_id=access.tenant_id,
            connection_id=connection_id,
            actor=user_actor(request, access.user.id),
        )
        return _to_model(await _load(session, connection_id))

    from cloudoptics.platform.jobs import get_job_app

    job_app = get_job_app()
    async with job_app.open_async():
        with contextlib.suppress(AlreadyEnqueued):  # one queued validation is enough
            await job_app.configure_task(
                "connectors:connection.validate",
                queueing_lock=f"validate:{connection_id}",
            ).defer_async(tenant_id=str(access.tenant_id), connection_id=str(connection_id))
    await session.execute(
        text("UPDATE cloud_connection SET status = 'validating' WHERE id = :c"),
        {"c": connection_id},
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return ValidationAccepted(connection_id=connection_id)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    connection_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> Response:
    row = await _load(session, connection_id)
    await session.execute(
        text("UPDATE cloud_connection SET status = 'disconnected' WHERE id = :c"),
        {"c": connection_id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="connection.disconnected",
        target_type="cloud_connection",
        target_id=str(connection_id),
        before={"status": row.status},
        after={"status": "disconnected"},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class CloudAccount(ApiModel):
    aws_account_id: str
    name: str | None
    account_type: Literal["management", "member", "standalone"]
    org_status: str | None
    org_seen_at: datetime | None
    resource_access: bool
    has_cost_data: bool


class AccountCoverage(ApiModel):
    accounts: int
    with_cost_data: int
    with_resource_access: int


class AccountList(ApiModel):
    """``coverage`` counts accounts still in the organization (US-1.3: "12 of 15")."""

    coverage: AccountCoverage
    accounts: list[CloudAccount]


@accounts_router.get("", response_model=AccountList)
async def list_cloud_accounts(access: ViewerAccess, session: Session) -> AccountList:
    rows, coverage = await accounts.list_accounts(session, datetime.now(UTC).date())
    return AccountList(
        coverage=AccountCoverage.model_validate(coverage),
        accounts=[CloudAccount.model_validate(r) for r in rows],
    )
