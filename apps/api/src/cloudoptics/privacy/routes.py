"""Deleting data: a disconnected connection's, or the whole workspace (PRD US-8.3, ADM-7).

- ``POST /v1/tenants/{t}/connections/{c}/delete-data`` deletes a disconnected connection's
  data (typed confirmation: its account ID: AWS account, Google Cloud billing account, Azure
  subscription or billing account, DigitalOcean team).
- ``POST /v1/tenants/{t}/deletion`` schedules the workspace's deletion after the grace
  period (typed confirmation: its name); ``GET`` shows it; ``DELETE`` cancels it.
- ``GET /v1/tenants/{t}/deletions`` lists every request with its verification record.

Admins only (the highest role; there is no separate owner).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import Field
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel, ApiProblem, user_actor
from cloudoptics.platform.settings import get_settings
from cloudoptics.privacy import service
from cloudoptics.privacy.removal import removal_steps
from cloudoptics.providers.naming import connection_label

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["privacy"])

LOAD_CONNECTION = text(
    "SELECT id, provider_account_id AS aws_account_id, provider, account_type, status "
    "FROM cloud_connection WHERE id = :c"
)


class DataDeletion(ApiModel):
    id: uuid.UUID
    kind: Literal["connection", "tenant"]
    connection_id: uuid.UUID | None
    # The connection's account ID (AWS account, Google Cloud billing account, Azure
    # subscription or billing account, DigitalOcean team) or the workspace name.
    target_label: str
    status: Literal["scheduled", "cancelled", "completed", "failed"]
    requested_at: datetime
    requested_by_email: str | None
    scheduled_for: datetime
    cancelled_at: datetime | None
    completed_at: datetime | None
    # Rows per table before and after, once completed.
    verification: dict[str, Any] | None
    error: str | None


class StackSteps(ApiModel):
    aws_account_id: str
    account_type: str
    stacks: list[str]


class ProviderRemoval(ApiModel):
    """What to remove in another cloud for one connection (privacy/removal.py)."""

    provider: str
    label: str
    steps: list[str]
    commands: list[str]


class TenantDeletion(ApiModel):
    """The workspace's scheduled deletion (``None`` when there is none), the grace period,
    the stacks to delete in AWS and what to remove in the other clouds (ours to point at,
    not to delete)."""

    deletion: DataDeletion | None
    stacks: list[StackSteps]
    removals: list[ProviderRemoval] = Field(default_factory=list)
    stacks_console_url: str = service.STACKS_URL
    grace_days: int


class ConnectionDeleteBody(ApiModel):
    # The connection's account ID, typed by the admin.
    confirm: str = Field(max_length=64)


class TenantDeleteBody(ApiModel):
    # The workspace name, typed by the admin.
    confirm_name: str = Field(max_length=200)


def _view(row: Any) -> DataDeletion:
    return DataDeletion.model_validate(row._mapping)


async def _one(session: Session, tenant_id: uuid.UUID, deletion_id: uuid.UUID) -> DataDeletion:
    rows = await service.list_deletions(session, tenant_id, deletion_id=deletion_id)
    return _view(rows[0])


async def _tenant_view(
    session: Session, tenant_id: uuid.UUID, deletion: DataDeletion | None
) -> TenantDeletion:
    steps = await service.stack_steps(session)
    removals = await removal_steps(session, tenant_id)
    return TenantDeletion(
        deletion=deletion,
        stacks=[StackSteps.model_validate(s, from_attributes=True) for s in steps],
        removals=[ProviderRemoval.model_validate(r, from_attributes=True) for r in removals],
        grace_days=get_settings().tenant_deletion_grace_days,
    )


@router.get("/deletions", response_model=list[DataDeletion])
async def list_deletions(access: AdminAccess, session: Session) -> list[DataDeletion]:
    return [_view(r) for r in await service.list_deletions(session, access.tenant_id)]


@router.post("/connections/{connection_id}/delete-data", response_model=DataDeletion)
async def delete_connection_data(
    connection_id: uuid.UUID,
    body: ConnectionDeleteBody,
    request: Request,
    response: Response,
    access: AdminAccess,
    session: Session,
) -> DataDeletion:
    conn = (await session.execute(LOAD_CONNECTION, {"c": connection_id})).one_or_none()
    if conn is None:
        raise ApiProblem(status.HTTP_404_NOT_FOUND, "not_found", "Connection not found")
    account = str(conn.aws_account_id).strip()
    label = connection_label(conn.provider, account, conn.account_type)
    if conn.status != "disconnected":
        raise ApiProblem(
            status.HTTP_409_CONFLICT,
            "connection_not_disconnected",
            f"Disconnect {label} before deleting its data.",
        )
    if body.confirm.strip() != account:
        raise ApiProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "confirmation_mismatch",
            f"Type the {label.removesuffix(account).strip()} ID {account} to confirm.",
        )
    open_requests = await service.list_deletions(
        session, access.tenant_id, kind="connection", status="scheduled"
    )
    existing = next((r for r in open_requests if r.connection_id == connection_id), None)
    if existing is not None:
        response.status_code = status.HTTP_202_ACCEPTED
        return _view(existing)

    now = datetime.now(UTC)
    actor = user_actor(request, access.user.id)
    deletion_id = await service.create_request(
        session,
        access.tenant_id,
        kind="connection",
        label=account,
        by=access.user.id,
        at=now,
        connection_id=connection_id,
    )
    if get_settings().jobs_mode == "inline":
        outcome = await service.run_due(session, access.tenant_id, deletion_id, now, actor)
        if outcome.email is not None:
            await service.send(outcome.email, session, access.tenant_id)
        return await _one(session, access.tenant_id, deletion_id)

    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=actor,
        action="connection.data_deletion_requested",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"awsAccountId": account, "provider": conn.provider},
    )
    from cloudoptics.platform.jobs import get_job_app
    from cloudoptics.privacy.jobs import defer_deletion

    await defer_deletion(get_job_app(), str(access.tenant_id), str(deletion_id))
    response.status_code = status.HTTP_202_ACCEPTED
    return await _one(session, access.tenant_id, deletion_id)


@router.get("/deletion", response_model=TenantDeletion)
async def get_tenant_deletion(access: AdminAccess, session: Session) -> TenantDeletion:
    rows = await service.list_deletions(
        session, access.tenant_id, kind="tenant", status="scheduled"
    )
    return await _tenant_view(session, access.tenant_id, _view(rows[0]) if rows else None)


@router.post("/deletion", response_model=TenantDeletion, status_code=status.HTTP_202_ACCEPTED)
async def schedule_tenant_deletion(
    body: TenantDeleteBody, request: Request, access: AdminAccess, session: Session
) -> TenantDeletion:
    tenant = (await session.execute(service.TENANT_NAME, {"t": access.tenant_id})).one()
    if body.confirm_name.strip() != tenant.name.strip():
        raise ApiProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "confirmation_mismatch",
            "Type the workspace name exactly as shown to confirm.",
        )
    if await service.list_deletions(session, access.tenant_id, kind="tenant", status="scheduled"):
        raise ApiProblem(
            status.HTTP_409_CONFLICT,
            "deletion_already_scheduled",
            "This workspace is already scheduled for deletion.",
        )
    when = datetime.now(UTC) + service.grace_period()
    deletion_id = await service.create_request(
        session, access.tenant_id, kind="tenant", label=tenant.name, by=access.user.id, at=when
    )
    await service.mark_tenant_deleting(session, access.tenant_id)
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="tenant.deletion_scheduled",
        target_type="tenant",
        target_id=str(access.tenant_id),
        before={"status": tenant.status},
        after={"status": "deleting", "scheduledFor": when.isoformat()},
    )
    view = await _tenant_view(
        session, access.tenant_id, await _one(session, access.tenant_id, deletion_id)
    )
    steps = await service.stack_steps(session)
    removals = await removal_steps(session, access.tenant_id)
    who = access.user.display_name or access.user.email
    email = service.scheduled_email(
        await service.admin_emails(session, access.tenant_id),
        tenant.name,
        when,
        who,
        steps,
        removals,
    )
    await service.send(email, session, access.tenant_id)
    return view


@router.delete("/deletion", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_tenant_deletion(
    request: Request, access: AdminAccess, session: Session
) -> Response:
    rows = await service.list_deletions(
        session, access.tenant_id, kind="tenant", status="scheduled"
    )
    if not rows or not await service.cancel(session, access.tenant_id, rows[0].id, access.user.id):
        raise ApiProblem(
            status.HTTP_404_NOT_FOUND,
            "no_deletion_scheduled",
            "This workspace is not scheduled for deletion.",
        )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="tenant.deletion_cancelled",
        target_type="tenant",
        target_id=str(access.tenant_id),
        before={"status": "deleting"},
        after={"status": "active"},
    )
    tenant = (await session.execute(service.TENANT_NAME, {"t": access.tenant_id})).one()
    who = access.user.display_name or access.user.email
    email = service.cancelled_email(
        await service.admin_emails(session, access.tenant_id), tenant.name, who
    )
    await service.send(email, session, access.tenant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
