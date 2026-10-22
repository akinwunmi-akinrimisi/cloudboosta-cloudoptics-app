"""Request dependencies: the signed-in user and their access to a tenant (RBAC)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.identity.auth import Claims, bearer_claims
from cloudoptics.platform.db import scope, session_dependency
from cloudoptics.platform.ids import uuid7

Role = Literal["admin", "member", "viewer"]
ROLE_RANK: dict[str, int] = {"viewer": 1, "member": 2, "admin": 3}

Session = Annotated[AsyncSession, Depends(session_dependency)]


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str
    display_name: str | None


async def current_user(
    claims: Annotated[Claims, Depends(bearer_claims)], session: Session
) -> CurrentUser:
    """Find or create the user for this token (just-in-time provisioning)."""
    row = (
        await session.execute(
            text(
                "INSERT INTO app_user (id, email, display_name, auth_subject) "
                "VALUES (:id, :email, :name, :sub) "
                "ON CONFLICT (auth_subject) DO UPDATE SET email = EXCLUDED.email, "
                "display_name = COALESCE(EXCLUDED.display_name, app_user.display_name) "
                "RETURNING id, email, display_name"
            ),
            {"id": uuid7(), "email": claims.email, "name": claims.name, "sub": claims.subject},
        )
    ).one()
    await scope(session, user_id=row.id)
    return CurrentUser(id=row.id, email=row.email, display_name=row.display_name)


User = Annotated[CurrentUser, Depends(current_user)]


@dataclass(frozen=True)
class TenantAccess:
    tenant_id: uuid.UUID
    user: CurrentUser
    role: Role


def require_role(minimum: Role):  # type: ignore[no-untyped-def]
    """Dependency factory: the user must belong to ``tenant_id`` with at least ``minimum``.

    Unknown tenants and tenants the user is not in both return 404, so tenant IDs cannot be
    probed. Too-low roles return 403.
    """

    async def dependency(tenant_id: uuid.UUID, user: User, session: Session) -> TenantAccess:
        await scope(session, user_id=user.id, tenant_id=tenant_id)
        role = (
            await session.execute(
                text("SELECT role FROM membership WHERE tenant_id = :t AND user_id = :u"),
                {"t": tenant_id, "u": user.id},
            )
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
        if ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires the {minimum} role")
        return TenantAccess(tenant_id=tenant_id, user=user, role=role)

    return dependency


ViewerAccess = Annotated[TenantAccess, Depends(require_role("viewer"))]
MemberAccess = Annotated[TenantAccess, Depends(require_role("member"))]
AdminAccess = Annotated[TenantAccess, Depends(require_role("admin"))]
