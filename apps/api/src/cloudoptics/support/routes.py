"""/v1/admin/support-access: the staff break-glass tool (WBS ADM-6, docs/11b §7.4).

Staff only (platform-admin allowlist, verified email) with an MFA session. A grant starts
at once (no tenant approval, see ``service``); the workspace's admins are emailed and the
workspace's audit log records the grant and its end. Every action also goes to the
platform audit chain (docs/11b §7.4a).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import Field, field_validator

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.identity.deps import (
    CurrentUser,
    Session,
    User,
    has_mfa,
    is_platform_admin,
    mfa_required_problem,
)
from cloudoptics.platform.api import ApiModel, ApiProblem, client_ip
from cloudoptics.platform.db import scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.support import service

router = APIRouter(prefix="/v1/admin/support-access", tags=["support"])


@dataclass(frozen=True)
class StaffSession:
    user: CurrentUser
    actor: Actor


async def support_staff(user: User, request: Request, session: Session) -> StaffSession:
    """Cloudoptics staff with a multi-factor session; opens the console's read scope."""
    if not is_platform_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cloudoptics staff only")
    if not await has_mfa(user):
        raise mfa_required_problem()
    await service.console_scope(session)
    actor = Actor(
        type="staff",
        id=user.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return StaffSession(user=user, actor=actor)


Staff = Annotated[StaffSession, Depends(support_staff)]


class GrantCreate(ApiModel):
    tenant_id: uuid.UUID
    justification: str = Field(min_length=10, max_length=2000)
    ticket_ref: str = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(
        default=service.DEFAULT_MINUTES, ge=service.MIN_MINUTES, le=service.MAX_MINUTES
    )

    @field_validator("justification")
    @classmethod
    def _justification(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 10:
            raise ValueError("explain why access is needed (at least 10 characters)")
        return value

    @field_validator("ticket_ref")
    @classmethod
    def _ticket(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("give the support ticket reference")
        return value


class Grant(ApiModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    staff_email: str
    staff_name: str | None
    justification: str
    ticket_ref: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    ended_by_email: str | None
    status: Literal["active", "ended", "expired"]
    requests: int


async def _grant(session: Session, grant_id: uuid.UUID) -> Grant:
    row = (await session.execute(service.ONE_GRANT, {"id": grant_id})).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Support access not found")
    return Grant.model_validate(row)


@router.get("", response_model=list[Grant])
async def list_grants(staff: Staff, session: Session) -> list[Grant]:
    """Every staff member's grants, newest first (the last 100)."""
    rows = await session.execute(service.LIST_GRANTS, {"n": 100})
    return [Grant.model_validate(r) for r in rows]


@router.post("", response_model=Grant, status_code=status.HTTP_201_CREATED)
async def create_grant(body: GrantCreate, staff: Staff, session: Session) -> Grant:
    """Open read-only access to one workspace now, for ``durationMinutes`` (15 to 120)."""
    await scope(session, user_id=staff.user.id, tenant_id=body.tenant_id)
    tenant = (await session.execute(service.TENANT, {"t": body.tenant_id})).one_or_none()
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    if await service.active_grant(session, body.tenant_id, staff.user.id):
        raise ApiProblem(
            status.HTTP_409_CONFLICT,
            "support_access_active",
            "You already have support access to this workspace. End it first.",
        )
    grant_id = (
        await session.execute(
            service.INSERT_GRANT,
            {
                "id": uuid7(),
                "t": body.tenant_id,
                "name": tenant.name,
                "u": staff.user.id,
                "why": body.justification,
                "ticket": body.ticket_ref,
                "minutes": body.duration_minutes,
            },
        )
    ).scalar_one()
    grant = await _grant(session, grant_id)
    admins = [r.email for r in await session.execute(service.ADMIN_EMAILS, {"t": body.tenant_id})]
    email = service.granted_email(
        admins,
        tenant.name,
        staff.user.display_name or staff.user.email,
        body.justification,
        body.ticket_ref,
        grant.expires_at,
    )
    await service.notify_admins(session, body.tenant_id, email)
    # Audit last: the chain's advisory lock is then held only until commit.
    after: dict[str, object] = {
        "justification": body.justification,
        "ticketRef": body.ticket_ref,
        "durationMinutes": body.duration_minutes,
        "expiresAt": grant.expires_at.isoformat(),
        "readOnly": True,
    }
    await _audit(session, staff, grant, "support_access.granted", after)
    return grant


@router.post("/{grant_id}/end", response_model=Grant)
async def end_grant(grant_id: uuid.UUID, staff: Staff, session: Session) -> Grant:
    """End an active grant now (any staff member may end any grant)."""
    found = await _grant(session, grant_id)
    await scope(session, user_id=staff.user.id, tenant_id=found.tenant_id)
    ended = (
        await session.execute(service.END_GRANT, {"id": grant_id, "u": staff.user.id})
    ).one_or_none()
    if ended is None:
        raise ApiProblem(
            status.HTTP_409_CONFLICT, "support_access_inactive", "This access has already ended."
        )
    grant = await _grant(session, grant_id)
    after: dict[str, object] = {
        "endedAt": grant.ended_at.isoformat() if grant.ended_at else None,
        "requests": grant.requests,
    }
    await _audit(session, staff, grant, "support_access.ended", after)
    return grant


async def _audit(
    session: Session, staff: StaffSession, grant: Grant, action: str, after: dict[str, object]
) -> None:
    """The workspace's chain (the tenant sees it) and the platform chain."""
    for tenant_id, extra in ((grant.tenant_id, {}), (None, {"tenantId": str(grant.tenant_id)})):
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor=staff.actor,
            action=action,
            target_type="support_access_grant",
            target_id=str(grant.id),
            after={**after, **extra},
        )
