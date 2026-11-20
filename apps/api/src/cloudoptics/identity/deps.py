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

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analytics import funnel
from cloudoptics.identity.auth import Claims, ProviderError, bearer_claims, get_verifier
from cloudoptics.platform import ratelimit
from cloudoptics.platform.api import ApiProblem
from cloudoptics.platform.db import scope, session_dependency
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.support import service as support

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


def is_platform_admin(user: CurrentUser) -> bool:
    """Cloudoptics staff (``CLOUDOPTICS_PLATFORM_ADMIN_EMAILS``), with the email confirmed:
    anyone can sign up with an address they do not own."""
    return user.email_verified and user.email.lower() in get_settings().platform_admins


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
# The app role cannot read or write other users' rows (RLS on app_user, migration 0041), so
# the collision check, the release and the upsert run in one SECURITY DEFINER function.
SIGN_IN_SQL = (
    "SELECT id, email, display_name, outcome, released, created "
    "FROM app_user_sign_in(:id, :sub, :email, :name, :verified)"
)


def email_in_use_problem() -> ApiProblem:
    return ApiProblem(status.HTTP_409_CONFLICT, "email_in_use", EMAIL_IN_USE_TITLE)


async def current_user(
    claims: Annotated[Claims, Depends(bearer_claims)], session: Session
) -> CurrentUser:
    """Find or create the user for this token (just-in-time provisioning), applying the
    email collision policy (module docstring). Counts the request against the user's rate
    limit first."""
    await ratelimit.per_user(claims.subject)
    row = (
        await session.execute(
            text(SIGN_IN_SQL),
            {
                "id": uuid7(),
                "email": claims.email,
                "name": claims.name,
                "sub": claims.subject,
                "verified": claims.email_verified,
            },
        )
    ).one()
    if row.released is not None:
        log.warning("Verified sign-in took an unverified email over from user %s", row.released)
    if row.outcome != "ok":
        raise email_in_use_problem()
    await scope(session, user_id=row.id)
    if row.created:
        await funnel.emit(session, "signup_completed", user_id=row.id)
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
    # Set when a Cloudoptics staff member is here under a break-glass grant (read-only).
    support_grant_id: uuid.UUID | None = None
    # Set inside a connect-invite session (ADR-022): ``user`` is then the admin who sent the
    # invite (connections record them as ``created_by``), and the person acting is the invite.
    connect_invite_id: uuid.UUID | None = None

    @property
    def binding_id(self) -> uuid.UUID:
        """What a sign-in state is bound to: the invite in an invite session, else the user."""
        return self.connect_invite_id or self.user.id


SUPPORT_READ_ONLY_TITLE = "Cloudoptics support access is read-only"
MEMBERSHIP_SQL = (
    "SELECT m.role, t.mfa_required FROM membership m "
    "JOIN tenant t ON t.id = m.tenant_id "
    "WHERE m.tenant_id = :t AND m.user_id = :u"
)


async def _support_access(
    request: Request, tenant_id: uuid.UUID, user: CurrentUser, session: AsyncSession, minimum: Role
) -> TenantAccess:
    """No membership: a staff member with an active break-glass grant for this workspace
    may read it (viewer routes, safe methods); everyone else gets the same 404 as before."""
    grant = (
        await support.active_grant(session, tenant_id, user.id) if is_platform_admin(user) else None
    )
    if grant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    if not await has_mfa(user):
        raise mfa_required_problem()
    allowed = minimum == "viewer" and request.method in support.SAFE_METHODS
    await support.record_request(
        grant.id, tenant_id, request.method, request.url.path, allowed=allowed
    )
    if not allowed:
        raise ApiProblem(status.HTTP_403_FORBIDDEN, "support_read_only", SUPPORT_READ_ONLY_TITLE)
    await support.make_read_only(session)
    return TenantAccess(tenant_id=tenant_id, user=user, role="viewer", support_grant_id=grant.id)


def require_role(minimum: Role):  # type: ignore[no-untyped-def]
    """Dependency factory: the user must belong to ``tenant_id`` with at least ``minimum``.

    Unknown tenants and tenants the user is not in both return 404, so tenant IDs cannot be
    probed. If the tenant requires MFA and the session has none, 401 ``mfa_required``
    (routes outside a tenant, such as /v1/me, stay open so the app can explain). Too-low
    roles return 403.

    Cloudoptics staff with an active break-glass grant (``cloudoptics.support``) and an MFA
    session pass viewer routes for GET/HEAD only, as a viewer, in a read-only transaction;
    anything else is refused with 403 ``support_read_only``. Every such request is recorded.
    """

    async def dependency(
        tenant_id: uuid.UUID, request: Request, user: User, session: Session
    ) -> TenantAccess:
        await scope(session, user_id=user.id, tenant_id=tenant_id)
        row = (
            await session.execute(text(MEMBERSHIP_SQL), {"t": tenant_id, "u": user.id})
        ).one_or_none()
        if row is None:
            access = await _support_access(request, tenant_id, user, session, minimum)
            await ratelimit.per_tenant(tenant_id)
            return access
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
