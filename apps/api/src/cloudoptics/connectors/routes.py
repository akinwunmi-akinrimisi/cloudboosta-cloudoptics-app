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
from starlette.concurrency import run_in_threadpool

from cloudoptics import providers
from cloudoptics.audit import log as audit
from cloudoptics.connectors import accounts, aws
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import (
    LIST_CONNECTIONS,
    ConnectionNotFoundError,
    check_stack_removed,
    get_connection,
    get_session_factory,
    mark_disconnected,
    read_external_id,
    record_delete_ignored,
    run_validation,
    secret_tag,
)
from cloudoptics.identity.deps import AdminAccess, Session, TenantAccess, ViewerAccess
from cloudoptics.notifications.alerts import send_connection_alert
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.base import ProviderName
from cloudoptics.providers.registry import CONNECTION_PROVIDER

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
    stack_name: str
    # The single stack's version as last reported; None while on the older stacks.
    stack_version: str | None = None
    # The cloud (ADR-018); added after the other fields so AWS responses only gain this key.
    provider: ProviderName = "aws"
    # How the secret was obtained: ``oauth`` for a DigitalOcean sign-in (ADR-021), which the
    # Connections page offers to reconnect instead of replacing a token.
    credential_type: Literal["static", "oauth"] = "static"


class ConnectionSetup(ApiModel):
    connection: Connection
    external_id: str
    quick_create_url: str | None
    template_url: str
    template_download_url: str
    resource_template_url: str
    resource_template_download_url: str
    resource_quick_create_url: str | None
    # Management connections only: one stack that deploys the role to every member account.
    resource_stackset_quick_create_url: str | None
    resource_stackset_download_url: str
    stack_region: str = aws.STACK_REGION
    principal_arn: str
    # The single stack (cloudoptics-access): moves this connection to it, or sets it up again.
    access_quick_create_url: str | None
    access_template_url: str
    access_template_download_url: str
    callback_url: str
    connection_ref: str
    latest_stack_version: str = aws.ACCESS_TEMPLATE_VERSION
    # True when the stack is older than the latest version (or still the older stacks).
    update_available: bool
    stack_console_url: str
    # AWS's Update stack page for this stack with the latest template already chosen.
    stack_update_url: str


class AccountAccess(ApiModel):
    aws_account_id: str
    access: bool
    # The AWS error code when the resource role could not be assumed (e.g. AccessDenied).
    error: str | None
    # For a member account still without access: how AWS's rollout of the role is going.
    rollout: str | None = None


class ScanStarted(ApiModel):
    """``queued`` on staging and production; the result itself when jobs run inline.

    ``accounts`` is the role check done in the request, so the answer is immediate even
    when the full scan runs later on the worker.
    """

    status: Literal["queued", "succeeded", "partial", "skipped"]
    accounts: list[AccountAccess]


class ValidationAccepted(ApiModel):
    connection_id: uuid.UUID
    status: Literal["validating"] = "validating"


def _to_model(row: Any) -> Connection:
    return Connection.model_validate(row._mapping)


def access_link(
    settings: Any, *, external_id: str, connection_ref: str, tenant_id: uuid.UUID
) -> str | None:
    if not settings.cfn_quick_create_enabled:
        return None
    return aws.access_quick_create_url(
        settings.cfn_access_template_url,
        external_id=external_id,
        principal_arn=settings.cloudoptics_principal_arn,
        resource_template_url=settings.cfn_resource_template_url,
        callback_url=f"{settings.public_api_url}/v1/cfn/callback",
        connection_ref=connection_ref,
        tenant_id=tenant_id,
    )


def _setup(row: Any, external_id: str) -> ConnectionSetup:
    settings = get_settings()
    on_access_stack = row.stack_name == aws.ACCESS_STACK_NAME
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
        resource_quick_create_url=aws.resource_quick_create_url(
            settings.cfn_resource_template_url,
            external_id=external_id,
            principal_arn=settings.cloudoptics_principal_arn,
        )
        if settings.cfn_quick_create_enabled
        else None,
        resource_stackset_quick_create_url=aws.stackset_quick_create_url(
            # Hosted next to resource-role.yaml (infra/aws/bootstrap.py uploads both).
            settings.cfn_resource_template_url.rsplit("/", 1)[0] + "/resource-stackset.yaml",
            external_id=external_id,
            principal_arn=settings.cloudoptics_principal_arn,
            resource_template_url=settings.cfn_resource_template_url,
        )
        if settings.cfn_quick_create_enabled and row.account_type == "management"
        else None,
        resource_stackset_download_url=(
            f"{settings.cfn_template_download_base}/resource-stackset.yaml"
        ),
        principal_arn=settings.cloudoptics_principal_arn,
        access_quick_create_url=access_link(
            settings, external_id=external_id, connection_ref=f"c:{row.id}", tenant_id=row.tenant_id
        ),
        access_template_url=settings.cfn_access_template_url,
        access_template_download_url=(
            f"{settings.cfn_template_download_base}/cloudoptics-access.yaml"
        ),
        callback_url=f"{settings.public_api_url}/v1/cfn/callback",
        connection_ref=f"c:{row.id}",
        update_available=not on_access_stack
        or aws.version_tuple(row.stack_version) < aws.version_tuple(aws.ACCESS_TEMPLATE_VERSION),
        stack_console_url=aws.stack_console_url(row.stack_name),
        stack_update_url=aws.stack_update_url(settings.cfn_access_template_url),
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
    existing = (
        await session.execute(
            text(
                "SELECT id, status FROM cloud_connection WHERE provider = 'aws' "
                "AND provider_account_id = :acct"
            ),
            {"acct": body.aws_account_id},
        )
    ).one_or_none()
    if existing is not None and existing.status != "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This AWS account is already connected to this workspace. Use Set up again or "
            "Disconnect on the Connections page.",
        )
    if existing is not None:
        return await _reconnect(existing.id, body, external_id, request, access, session)
    connection_id = uuid7()
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    "INSERT INTO cloud_connection (id, tenant_id, provider_account_id, "
                    "account_type, "
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
            "This AWS account is already connected to this workspace.",
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


async def _reconnect(
    connection_id: uuid.UUID,
    body: ConnectionCreate,
    external_id: str,
    request: Request,
    access: TenantAccess,
    session: Session,
) -> ConnectionSetup:
    """A disconnected account connected again: same row (its cost history stays), fresh
    External ID and settings, back to pending until the new stack is checked."""
    await session.execute(
        text(
            "UPDATE cloud_connection SET status = 'pending', health = NULL, account_type = :type, "
            "role_arn = :arn, external_id_enc = :ext, stack_name = :stack, "
            "create_cur_export = :cur, cur_bucket = :bucket, cur_prefix = NULL, "
            "cur_region = NULL, cur_export_arn = NULL, permission_checks = '[]', "
            "last_error = NULL, last_validated_at = NULL, account_verified_at = NULL, "
            "pending_stack = NULL, created_by = :by WHERE id = :id"
        ),
        {
            "id": connection_id,
            "type": body.account_type,
            "arn": aws.billing_role_arn(body.aws_account_id),
            "ext": get_encryptor().encrypt(external_id, access.tenant_id),
            "stack": aws.STACK_NAME,
            "cur": body.create_cur_export and not body.existing_cur_bucket_name,
            "bucket": body.existing_cur_bucket_name,
            "by": access.user.id,
        },
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="connection.reconnected",
        target_type="cloud_connection",
        target_id=str(connection_id),
        before={"status": "disconnected"},
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
    if row.provider != "aws":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only AWS connections are set up with a stack."
        )
    return _setup(row, await read_external_id(session, row, access.tenant_id))


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
    row = await _load(session, connection_id)
    if row.status == "disconnected":
        # Disconnect is final; Reconnect gives the account a new External ID.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This connection is disconnected. Use Reconnect to connect the account again.",
        )
    if not await start_validation(
        session, access.tenant_id, connection_id, user_actor(request, access.user.id)
    ):
        return _to_model(await _load(session, connection_id))
    response.status_code = status.HTTP_202_ACCEPTED
    return ValidationAccepted(connection_id=connection_id)


async def start_validation(
    session: Session,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    actor: audit.Actor,
    *,
    delay_seconds: int = 0,
) -> bool:
    """Validate now (inline jobs, returns False) or queue it for the worker (returns True).

    ``delay_seconds`` holds a queued validation back (a stack's new role may take a moment
    to reach STS). A disconnected connection is never validated.
    """
    if get_settings().jobs_mode == "inline":
        # The connection's provider validates it (ADR-018); AWS runs run_validation as before.
        name = (await session.execute(CONNECTION_PROVIDER, {"c": connection_id})).scalar()
        plugin = providers.find(str(name or "aws"))
        if plugin is None or plugin.connect is None:
            return False
        if plugin.name == "aws":
            result: Any = await run_validation(
                session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
            )
        else:
            result = await plugin.connect.validate(
                session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
            )
        if result is not None and result.health_worsened:
            await send_connection_alert(session, tenant_id, connection_id)
        return False

    from cloudoptics.platform.jobs import get_job_app

    job_app = get_job_app()
    async with job_app.open_async():
        with contextlib.suppress(AlreadyEnqueued):  # one queued validation is enough
            await job_app.configure_task(
                "connectors:connection.validate",
                queueing_lock=f"validate:{connection_id}",
                schedule_in={"seconds": delay_seconds} if delay_seconds else None,
            ).defer_async(tenant_id=str(tenant_id), connection_id=str(connection_id))
    await session.execute(
        text(
            "UPDATE cloud_connection SET status = 'validating' "
            "WHERE id = :c AND status <> 'disconnected'"
        ),
        {"c": connection_id},
    )
    return True


async def start_removal_check(
    session: Session, tenant_id: uuid.UUID, conn: Any, stack_name: str
) -> str:
    """A stack said it was deleted: disconnect once the role can no longer be assumed.

    Inline jobs check straight away and answer ``disconnected`` or ``kept``; queued jobs
    answer ``verifying`` and the worker checks later (``connectors:connection.verify_removed``).
    """
    tag = secret_tag(get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id))
    if get_settings().jobs_mode == "inline":
        outcome, code = await check_stack_removed(
            session,
            tenant_id=tenant_id,
            connection_id=conn.id,
            stack_name=stack_name,
            tag=tag,
            final=True,
        )
        if outcome == "unknown":
            await record_delete_ignored(session, tenant_id, conn.id, code or "unknown")
            return "kept"
        return outcome

    from cloudoptics.connectors.jobs import defer_removal_check
    from cloudoptics.platform.jobs import get_job_app

    await defer_removal_check(
        get_job_app(), str(tenant_id), str(conn.id), stack_name, tag, attempt=1
    )
    return "verifying"


@router.post(
    "/{connection_id}/scan",
    response_model=ScanStarted,
    responses={202: {"model": ScanStarted}},
)
async def scan_resources(
    connection_id: uuid.UUID, response: Response, access: AdminAccess, session: Session
) -> ScanStarted:
    """Check the resource role now, then scan, instead of waiting for the 12-hourly scan."""
    from cloudoptics.inventory.scanner import check_access, scan_connection

    row = await _load(session, connection_id)
    if row.status not in accounts.USABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Check the billing connection first: resources are scanned for working connections.",
        )
    if row.provider != "aws":
        return await _scan_provider(row, response, access, session)
    account_ids = await accounts.scan_account_ids(session, connection_id)
    external_id = await read_external_id(session, row, access.tenant_id)
    factory = get_session_factory()
    checked: list[AccountAccess] = []
    for account_id in account_ids:
        error = await run_in_threadpool(
            check_access, factory, account_id, external_id, access.tenant_id, row.stack_name
        )
        checked.append(AccountAccess(aws_account_id=account_id, access=error is None, error=error))
    if (
        row.stack_name == aws.ACCESS_STACK_NAME
        and row.account_type == "management"
        and any(not a.access for a in checked)
    ):
        rollout = await run_in_threadpool(
            _member_rollout, factory, row.role_arn, external_id, access.tenant_id
        )
        for a in checked:
            if a.access or a.aws_account_id == row.aws_account_id.strip():
                continue
            if isinstance(rollout, str):
                a.rollout = await _rollout_problem(session, access.tenant_id, row.id, rollout)
            else:
                a.rollout = rollout.get(
                    a.aws_account_id,
                    "AWS has not started adding the role to this account yet.",
                )
    if not any(a.access for a in checked):
        return ScanStarted(status="skipped", accounts=checked)

    if get_settings().jobs_mode == "inline":
        from cloudoptics.analysis.engine import run_analysis
        from cloudoptics.metrics.puller import pull_connection

        result = await scan_connection(access.tenant_id, connection_id)
        if result in ("succeeded", "partial"):
            await pull_connection(access.tenant_id, connection_id)
            await run_analysis(access.tenant_id)
        return ScanStarted(status=result, accounts=checked)

    from cloudoptics.inventory.jobs import defer_scan
    from cloudoptics.inventory.scanner import queue_run
    from cloudoptics.platform.jobs import get_job_app

    # Show the access straight away; the scan on the worker confirms it and adds resources.
    for a in checked:
        await session.execute(
            text(
                "UPDATE cloud_account SET resource_access = :access WHERE connection_id = :c "
                "AND provider_account_id = :a"
            ),
            {"access": a.access, "c": connection_id, "a": a.aws_account_id},
        )
    # The Connections page shows it as running from now until the worker finishes it.
    await queue_run(session, access.tenant_id, connection_id, len(account_ids))
    await defer_scan(get_job_app(), str(access.tenant_id), str(connection_id))
    response.status_code = status.HTTP_202_ACCEPTED
    return ScanStarted(status="queued", accounts=checked)


async def _scan_provider(
    row: Any, response: Response, access: AdminAccess, session: Session
) -> ScanStarted:
    """Another cloud's scan through its plug-in (ADR-018): no AWS role check; the access
    shown is what the plug-in's last scan recorded per account."""
    plugin = providers.find(row.provider)
    if plugin is None or plugin.inventory is None:
        return ScanStarted(status="skipped", accounts=[])
    if get_settings().jobs_mode == "inline":
        from cloudoptics.analysis.engine import run_analysis

        result = await plugin.inventory.scan(access.tenant_id, row.id)
        if result in ("succeeded", "partial"):
            if plugin.metrics is not None:
                await plugin.metrics.pull(access.tenant_id, row.id)
            await run_analysis(access.tenant_id)
        listed = await session.execute(
            text(
                "SELECT provider_account_id, resource_access FROM cloud_account "
                "WHERE connection_id = :c AND account_type = 'member' ORDER BY 1"
            ),
            {"c": row.id},
        )
        return ScanStarted(
            status=result if result in ("succeeded", "partial") else "skipped",
            accounts=[
                AccountAccess(aws_account_id=a, access=ok, error=None if ok else "AccessDenied")
                for a, ok in listed
            ],
        )
    from cloudoptics.inventory.jobs import defer_scan
    from cloudoptics.platform.jobs import get_job_app

    await defer_scan(get_job_app(), str(access.tenant_id), str(row.id))
    response.status_code = status.HTTP_202_ACCEPTED
    return ScanStarted(status="queued", accounts=[])


def _member_rollout(
    factory: Any, role_arn: str, external_id: str, tenant_id: uuid.UUID
) -> dict[str, str] | str:
    try:
        payer = factory.assume(role_arn, external_id, aws.session_name(tenant_id, "rollout"))
    except Exception as exc:  # the billing check reports role problems; this is only a hint
        return type(exc).__name__
    return aws.member_rollout(payer)


async def _rollout_problem(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, code: str
) -> str:
    """Why the rollout cannot be read, with the stack's own last failure when it sent one."""
    last = (
        await session.execute(
            text(
                "SELECT after->>'reason' FROM audit_log WHERE tenant_id = :t AND action = "
                "'connection.member_rollout_failed' AND target_id = :c ORDER BY id DESC LIMIT 1"
            ),
            {"t": tenant_id, "c": str(connection_id)},
        )
    ).scalar_one_or_none()
    if code == "StackSetNotFoundException":
        words = "AWS has not set up the rollout to member accounts."
    else:
        words = f"Cloudoptics could not read the rollout to member accounts ({code})."
    if last:
        words += f" The stack's last try said: {last}"
    return words


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    connection_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> Response:
    row = await _load(session, connection_id)
    actor = user_actor(request, access.user.id)
    if row.credential_type == "oauth" and row.status != "disconnected":
        # A DigitalOcean sign-in (ADR-021): revoke the access at DigitalOcean, then the
        # tokens are wiped with every other provider secret (mark_disconnected).
        from cloudoptics.providers.digitalocean.oauth import revoke_stored

        revoked = await revoke_stored(session, access.tenant_id, row)
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=actor,
            action="connection.token_revoked",
            target_type="cloud_connection",
            target_id=str(connection_id),
            after={"provider": row.provider, "revoked": bool(revoked)},
        )
    await mark_disconnected(session, access.tenant_id, connection_id, row.status, actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class CloudAccount(ApiModel):
    connection_id: uuid.UUID
    aws_account_id: str
    name: str | None
    account_type: Literal["management", "member", "standalone"]
    org_status: str | None
    org_seen_at: datetime | None
    resource_access: bool
    has_cost_data: bool
    provider: ProviderName = "aws"


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
