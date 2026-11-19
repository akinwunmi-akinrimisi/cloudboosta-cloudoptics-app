"""Request dependencies: the signed-in user and their access to a tenant (RBAC + MFA policy).

Just-in-time users and email collisions. ``app_user`` is keyed by the provider's subject
and its email is unique too, so a new identity (or one whose email changed) can present an
email another subject's row still holds. Identities are never merged. The policy:

- the new identity's email is verified and the holder's is not: the email moves to the new
  identity, and the holder's row keeps its memberships under a placeholder address
  (``released+<user id>@email-released.invalid``) until that subject signs in again;
- any other case (the holder's email is verified, or the new one is not): the sign-in is
  refused with a 409 ``email_in_use`` problem, and the existing row is left as it is.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


EMAIL_IN_USE_TITLE = (
    "This email address is already linked to another sign-in. Sign in the way you did "
    "before, or contact support to move the address."
)
EMAIL_HOLDER_SQL = (
    "SELECT id, email_verified FROM app_user WHERE email = :email AND auth_subject <> :sub "
    "FOR UPDATE"
)
RELEASE_EMAIL_SQL = (
    "UPDATE app_user SET email = :placeholder, email_verified = false WHERE id = :id"
)
UPSERT_USER_SQL = (
    "INSERT INTO app_user (id, email, display_name, auth_subject, email_verified) "
    "VALUES (:id, :email, :name, :sub, :verified) "
    "ON CONFLICT (auth_subject) DO UPDATE SET email = EXCLUDED.email, "
    "email_verified = EXCLUDED.email_verified, "
    "display_name = COALESCE(EXCLUDED.display_name, app_user.display_name) "
    "RETURNING id, email, display_name"
)


def email_in_use_problem() -> ApiProblem:
    return ApiProblem(status.HTTP_409_CONFLICT, "email_in_use", EMAIL_IN_USE_TITLE)


async def _claim_email(session: AsyncSession, claims: Claims) -> None:
    """Apply the email collision policy (module docstring) before the upsert."""
    holder = (
        await session.execute(
            text(EMAIL_HOLDER_SQL), {"email": claims.email, "sub": claims.subject}
        )
    ).one_or_none()
    if holder is None:
        return
    if not claims.email_verified or holder.email_verified:
        raise email_in_use_problem()
    await session.execute(
        text(RELEASE_EMAIL_SQL),
        {"id": holder.id, "placeholder": f"released+{holder.id}@email-released.invalid"},
    )
    log.warning("Verified sign-in took an unverified email over from user %s", holder.id)


async def current_user(
    claims: Annotated[Claims, Depends(bearer_claims)], session: Session
) -> CurrentUser:
    """Find or create the user for this token (just-in-time provisioning). Counts the
    request against the user's rate limit first."""
    await ratelimit.per_user(claims.subject)
    await _claim_email(session, claims)
    try:
        # A savepoint: another sign-in can take the email between the check and here.
        async with session.begin_nested():
            row = (
                await session.execute(
                    text(UPSERT_USER_SQL),
                    {
                        "id": uuid7(),
                        "email": claims.email,
                        "name": claims.name,
                        "sub": claims.subject,
                        "verified": claims.email_verified,
                    },
                )
            ).one()
    except IntegrityError as exc:
        raise email_in_use_problem() from exc
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
