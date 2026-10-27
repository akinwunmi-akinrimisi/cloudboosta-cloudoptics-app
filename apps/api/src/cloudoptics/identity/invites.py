"""Invites and member management (WBS ADM-3, PRD US-8.1, docs/09 §3.2 "Tenants").

An admin invites an email address with a role. Nobody needs to click a link: the first
time someone signs in with that email, and the sign-in provider has confirmed they own it,
they join the workspace (``accept_invites``, run by ``GET /v1/me``). Invites expire after
7 days. Admins can change a member's role or remove them, but a workspace always keeps at
least one admin.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import EmailStr
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, CurrentUser, Role, Session
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.db import scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["identity"])

INVITE_DAYS = 7


class InviteCreate(ApiModel):
    email: EmailStr
    role: Role


class Invite(ApiModel):
    id: uuid.UUID
    email: str
    role: Role
    invited_by_email: str | None
    created_at: datetime
    expires_at: datetime
    status: Literal["pending", "expired"]
    # Whether the invite email went out (Postmark configured and it accepted the message).
    email_sent: bool | None = None


class MemberPatch(ApiModel):
    role: Role


SELECT_INVITES = text(
    "SELECT i.id, i.email, i.role, u.email AS invited_by_email, i.created_at, i.expires_at, "
    "CASE WHEN i.expires_at > now() THEN 'pending' ELSE 'expired' END AS status "
    "FROM invite i LEFT JOIN app_user u ON u.id = i.invited_by "
    # The tenant filter matters: RLS also lets a user see invites addressed to their own email.
    "WHERE i.tenant_id = :t AND i.accepted_at IS NULL AND i.revoked_at IS NULL "
    "AND (CAST(:id AS uuid) IS NULL OR i.id = :id) ORDER BY i.created_at DESC"
)


def invite_email(to: str, workspace: str, inviter: str, role: str, expires: datetime) -> Email:
    url = get_settings().app_url
    subject = f"{inviter} invited you to {workspace} on Cloudoptics"
    lines = [
        f"{inviter} invited you to the Cloudoptics workspace {workspace} as {role}.",
        "",
        f"Sign in or create an account with this email address ({to}) at {url} and you "
        "join the workspace straight away.",
        "",
        f"The invite expires on {expires:%d %B %Y}.",
    ]
    body = (
        f"<p>{html.escape(inviter)} invited you to the Cloudoptics workspace "
        f"<strong>{html.escape(workspace)}</strong> as {html.escape(role)}.</p>"
        f'<p><a href="{html.escape(url)}">Sign in or create an account</a> with this email '
        f"address ({html.escape(to)}) and you join the workspace straight away.</p>"
        f"<p>The invite expires on {expires:%d %B %Y}.</p>"
    )
    return Email([to], subject, "\n".join(lines), body, tag="invite")


@router.post(
    "/invites",
    response_model=Invite,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    body: InviteCreate, request: Request, access: AdminAccess, session: Session
) -> Invite:
    email = str(body.email).strip().lower()
    member = (
        await session.execute(
            text(
                "SELECT 1 FROM membership m JOIN app_user u ON u.id = m.user_id "
                "WHERE m.tenant_id = :t AND u.email = :e"
            ),
            {"t": access.tenant_id, "e": email},
        )
    ).first()
    if member:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{email} is already in this workspace")
    # Inviting the same address again replaces the open invite (new role, new expiry).
    await session.execute(
        text(
            "UPDATE invite SET revoked_at = now() WHERE tenant_id = :t AND email = :e "
            "AND accepted_at IS NULL AND revoked_at IS NULL"
        ),
        {"t": access.tenant_id, "e": email},
    )
    invite_id = uuid7()
    expires = datetime.now(UTC) + timedelta(days=INVITE_DAYS)
    await session.execute(
        text(
            "INSERT INTO invite (id, tenant_id, email, role, invited_by, expires_at) "
            "VALUES (:id, :t, :e, :r, :u, :x)"
        ),
        {
            "id": invite_id,
            "t": access.tenant_id,
            "e": email,
            "r": body.role,
            "u": access.user.id,
            "x": expires,
        },
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="invite.created",
        target_type="invite",
        target_id=str(invite_id),
        after={"email": email, "role": body.role, "expiresAt": expires.isoformat()},
    )
    workspace = (
        await session.execute(
            text("SELECT name FROM tenant WHERE id = :t"), {"t": access.tenant_id}
        )
    ).scalar_one()
    inviter = access.user.display_name or access.user.email
    sent = True
    try:
        await get_mailer().send(invite_email(email, workspace, inviter, body.role, expires))
    except EmailError as exc:
        sent = False
        log.warning("invite email to tenant %s not sent: %s", access.tenant_id, exc)
    row = (await session.execute(SELECT_INVITES, {"id": invite_id, "t": access.tenant_id})).one()
    return Invite(**row._mapping, email_sent=sent)


@router.get("/invites", response_model=list[Invite], response_model_by_alias=True)
async def list_invites(access: AdminAccess, session: Session) -> list[Invite]:
    rows = await session.execute(SELECT_INVITES, {"id": None, "t": access.tenant_id})
    return [Invite.model_validate(r._mapping) for r in rows]


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> Response:
    email = (
        await session.execute(
            text(
                "UPDATE invite SET revoked_at = now() WHERE id = :id "
                "AND accepted_at IS NULL AND revoked_at IS NULL RETURNING email"
            ),
            {"id": invite_id},
        )
    ).scalar_one_or_none()
    if email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="invite.revoked",
        target_type="invite",
        target_id=str(invite_id),
        before={"email": email},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _member_role(session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """The member's role. Locks the workspace's memberships first, so two changes at once
    cannot both remove the last admin."""
    roles: dict[uuid.UUID, str] = dict(
        (
            await session.execute(
                text("SELECT user_id, role FROM membership WHERE tenant_id = :t FOR UPDATE"),
                {"t": tenant_id},
            )
        ).all()
    )
    if user_id not in roles:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    return str(roles[user_id])


async def _keeps_an_admin(session: Session, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    others = (
        await session.execute(
            text(
                "SELECT count(*) FROM membership WHERE tenant_id = :t "
                "AND role = 'admin' AND user_id <> :u"
            ),
            {"t": tenant_id, "u": user_id},
        )
    ).scalar_one()
    if not others:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A workspace needs at least one admin. Make someone else admin first.",
        )


@router.patch("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def change_role(
    user_id: uuid.UUID, body: MemberPatch, request: Request, access: AdminAccess, session: Session
) -> Response:
    role = await _member_role(session, access.tenant_id, user_id)
    if role == body.role:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if role == "admin":
        await _keeps_an_admin(session, access.tenant_id, user_id)
    await session.execute(
        text("UPDATE membership SET role = :r WHERE tenant_id = :t AND user_id = :u"),
        {"r": body.role, "t": access.tenant_id, "u": user_id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="member.role_changed",
        target_type="user",
        target_id=str(user_id),
        before={"role": role},
        after={"role": body.role},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> Response:
    role = await _member_role(session, access.tenant_id, user_id)
    if role == "admin":
        await _keeps_an_admin(session, access.tenant_id, user_id)
    await session.execute(
        text("DELETE FROM membership WHERE tenant_id = :t AND user_id = :u"),
        {"t": access.tenant_id, "u": user_id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="member.removed",
        target_type="user",
        target_id=str(user_id),
        before={"role": role},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def accept_invites(request: Request, user: CurrentUser, session: Session) -> None:
    """Join every workspace with an open invite to this user's confirmed email."""
    if not user.email_verified:
        return
    invites = (
        await session.execute(
            text(
                "SELECT id, tenant_id, role FROM invite WHERE email = :e "
                "AND accepted_at IS NULL AND revoked_at IS NULL "
                "AND expires_at > now() ORDER BY created_at"
            ),
            {"e": user.email},
        )
    ).all()
    for invite in invites:
        await scope(session, user_id=user.id, tenant_id=invite.tenant_id)
        joined = (
            await session.execute(
                text(
                    "INSERT INTO membership (tenant_id, user_id, role) VALUES (:t, :u, :r) "
                    "ON CONFLICT DO NOTHING RETURNING role"
                ),
                {"t": invite.tenant_id, "u": user.id, "r": invite.role},
            )
        ).first()
        await session.execute(
            text("UPDATE invite SET accepted_at = now(), accepted_by = :u WHERE id = :id"),
            {"u": user.id, "id": invite.id},
        )
        if joined:
            await audit.record(
                session,
                tenant_id=invite.tenant_id,
                actor=user_actor(request, user.id),
                action="member.joined",
                target_type="invite",
                target_id=str(invite.id),
                after={"email": user.email, "role": invite.role},
            )
    if invites:
        await scope(session, user_id=user.id)
