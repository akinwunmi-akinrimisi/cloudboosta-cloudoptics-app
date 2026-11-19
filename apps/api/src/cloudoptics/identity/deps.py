"""Request dependencies: the signed-in user and their access to a tenant (RBAC + MFA policy)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.identity.auth import Claims, ProviderError, bearer_claims, get_verifier
from cloudoptics.platform import ratelimit
from cloudoptics.platform.api import ApiProblem
from cloudoptics.platform.db import scope, session_dependency
from cloudoptics.platform.ids import uuid7

Role = Literal["admin", "member", "viewer"]
ROLE_RANK: dict[str, int] = {"viewer": 1, "member": 2, "admin": 3}

Session = Annotated[AsyncSession, Depends(session_dependency)]

log = logging.getLogger(__name__)

MFA_REQUIRED_TITLE = (
    "This workspace requires multi-factor authentication. Turn on MFA for your account, "
    "then sign out and sign in again."
)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    email: str
    display_name: str | None
    email_verified: bool = False
    auth_subject: str = ""
    # From the token; None means ask the identity provider (see has_mfa).
    mfa: bool | None = False


async def has_mfa(user: CurrentUser) -> bool:
    """Whether this session meets an MFA requirement (docs/06 FR-SEC-05)."""
    if user.mfa is not None:
        return user.mfa
    try:
        return await get_verifier().mfa_enrolled(user.auth_subject)
    except ProviderError as exc:
        log.error("MFA lookup failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Could not check multi-factor authentication with the sign-in provider",
        ) from exc


def mfa_required_problem() -> ApiProblem:
    # RFC 9470 (OAuth step-up): 401 with error="insufficient_user_authentication".
    return ApiProblem(
        status.HTTP_401_UNAUTHORIZED,
        "mfa_required",
        MFA_REQUIRED_TITLE,
        headers={
            "WWW-Authenticate": 'Bearer error="insufficient_user_authentication", '
            'error_description="Multi-factor authentication is required"'
        },
    )


async def current_user(
    claims: Annotated[Claims, Depends(bearer_claims)], session: Session
) -> CurrentUser:
    """Find or create the user for this token (just-in-time provisioning). Counts the
    request against the user's rate limit first."""
    await ratelimit.per_user(claims.subject)
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
    return CurrentUser(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        email_verified=claims.email_verified,
        auth_subject=claims.subject,
        mfa=claims.mfa,
    )


User = Annotated[CurrentUser, Depends(current_user)]


@dataclass(frozen=True)
class TenantAccess:
    tenant_id: uuid.UUID
    user: CurrentUser
    role: Role


def require_role(minimum: Role):  # type: ignore[no-untyped-def]
    """Dependency factory: the user must belong to ``tenant_id`` with at least ``minimum``.

    Unknown tenants and tenants the user is not in both return 404, so tenant IDs cannot be
    probed. If the tenant requires MFA and the session has none, 401 ``mfa_required``
    (routes outside a tenant, such as /v1/me, stay open so the app can explain). Too-low
    roles return 403.
    """

    async def dependency(tenant_id: uuid.UUID, user: User, session: Session) -> TenantAccess:
        await scope(session, user_id=user.id, tenant_id=tenant_id)
        row = (
            await session.execute(
                text(
                    "SELECT m.role, t.mfa_required FROM membership m "
                    "JOIN tenant t ON t.id = m.tenant_id "
                    "WHERE m.tenant_id = :t AND m.user_id = :u"
                ),
                {"t": tenant_id, "u": user.id},
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
        role = row.role
        if row.mfa_required and not await has_mfa(user):
            raise mfa_required_problem()
        if ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires the {minimum} role")
        # Only members count against a workspace's limit, so outsiders cannot use it up.
        await ratelimit.per_tenant(tenant_id)
        return TenantAccess(tenant_id=tenant_id, user=user, role=role)

    return dependency


ViewerAccess = Annotated[TenantAccess, Depends(require_role("viewer"))]
MemberAccess = Annotated[TenantAccess, Depends(require_role("member"))]
AdminAccess = Annotated[TenantAccess, Depends(require_role("admin"))]
