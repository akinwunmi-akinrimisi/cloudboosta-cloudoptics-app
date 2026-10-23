"""/v1/tenants/{t}/audit-log: the workspace's audit trail, newest first (PRD FR-SEC-05)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import text

from cloudoptics.audit.log import verify_chain
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel

router = APIRouter(prefix="/v1/tenants/{tenant_id}/audit-log", tags=["audit"])


class AuditActor(ApiModel):
    type: Literal["user", "system", "staff"]
    email: str | None
    name: str | None


class AuditEntry(ApiModel):
    id: int
    occurred_at: datetime
    actor: AuditActor
    action: str
    target_type: str | None
    target_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    ip: str | None


class AuditPage(ApiModel):
    """``nextBefore`` pages back; ``verified`` is false if any entry was altered or removed."""

    entries: list[AuditEntry]
    next_before: int | None
    verified: bool


@router.get("", response_model=AuditPage)
async def audit_log(
    access: AdminAccess,
    session: Session,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditPage:
    rows = (
        await session.execute(
            text(
                "SELECT a.id, a.occurred_at, a.actor_type, u.email, u.display_name, a.action, "
                "a.target_type, a.target_id, a.before, a.after, host(a.ip) AS ip "
                "FROM audit_log a LEFT JOIN app_user u ON u.id = a.actor_id "
                "WHERE a.tenant_id = :t AND (CAST(:before AS bigint) IS NULL OR a.id < :before) "
                "ORDER BY a.id DESC LIMIT :n"
            ),
            {"t": access.tenant_id, "before": before, "n": limit + 1},
        )
    ).all()
    entries = [
        AuditEntry(
            id=r.id,
            occurred_at=r.occurred_at,
            actor=AuditActor(type=r.actor_type, email=r.email, name=r.display_name),
            action=r.action,
            target_type=r.target_type,
            target_id=r.target_id,
            before=r.before,
            after=r.after,
            ip=r.ip,
        )
        for r in rows[:limit]
    ]
    return AuditPage(
        entries=entries,
        next_before=entries[-1].id if len(rows) > limit else None,
        verified=await verify_chain(session, access.tenant_id),
    )
