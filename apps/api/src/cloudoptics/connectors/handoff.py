"""Send the connect step to the workspace's cloud admin (connect invites, ADR-022).

The person who signs up often cannot connect the cloud themselves. A workspace admin enters
their cloud admin's email and a provider; the admin gets an email with a link that opens only
that cloud's connect step. They finish it without an account, without joining the workspace
and without seeing any cost data.

Sender routes (``/v1/tenants/{t}/connect-invites``, workspace admins): send, list with status
(sent, opened, connected, expired, revoked), revoke. At most ``connect_invites_per_day`` sends
per workspace in 24 hours.

Invite routes (``/v1/connect-invite``, not signed in, limited per client address):

- ``POST /open`` trades the link's token for a session token. Each open makes a new session
  and ends the previous one, so a link forwarded on is only as good as its newest holder.
- Every other route takes ``Authorization: ConnectInvite <session token>`` through
  ``invite_session``, the only dependency that accepts it. Workspace routes take a signed-in
  user's bearer token through ``require_role``, so the session can read no costs,
  recommendations, members, settings or exports (tests/test_connect_invites.py tries every
  other route with it).
- The session reaches only its own provider's connect actions, only on the connection it
  created, and Check access. It runs the same handlers as the in-app connect steps, with a
  ``TenantAccess`` whose ``user`` is the sender (connections record them as ``created_by``)
  and whose ``connect_invite_id`` is set: audit rows name the invite (``user_actor``) and the
  DigitalOcean sign-in state is bound to the invite, not the sender (``binding_id``).

A link stops working when it expires (7 days), is revoked, its sender is no longer an admin
of the workspace, or it has connected an account ("single use"). After connecting, the
session can still read its result and run Check access again, nothing else.
"""

from __future__ import annotations

import hashlib
import html
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors import access as intents
from cloudoptics.connectors import accounts
from cloudoptics.connectors import routes as connections
from cloudoptics.connectors.routes import Connection, ValidationAccepted
from cloudoptics.connectors.service import ConnectionNotFoundError, get_connection
from cloudoptics.identity.deps import AdminAccess, CurrentUser, Session, TenantAccess
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform import ratelimit
from cloudoptics.platform.api import ApiModel, ApiProblem, user_actor
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.azure import routes as azure
from cloudoptics.providers.base import ProviderName
from cloudoptics.providers.digitalocean import routes as digitalocean
from cloudoptics.providers.gcp import routes as gcp
from cloudoptics.providers.naming import provider_label

log = logging.getLogger(__name__)

INVITE_DAYS = 7
SESSION_SCHEME = "connectinvite"

router = APIRouter(prefix="/v1/tenants/{tenant_id}/connect-invites", tags=["connections"])
invite_router = APIRouter(
    prefix="/v1/connect-invite",
    tags=["connections"],
    dependencies=[
        Depends(
            ratelimit.per_ip("connect-invite", lambda: get_settings().rate_limit_public_per_minute)
        )
    ],
)

InviteStatus = Literal["sent", "opened", "connected", "expired", "revoked"]

# What the email and the landing page say each cloud's admin will grant (read-only).
GRANTS: dict[str, str] = {
    "aws": "a read-only IAM role that lets Cloudoptics read the bills, budgets, Savings Plans, "
    "resources and their metrics",
    "gcp": "read access to the billing export in BigQuery and a viewer role on your projects",
    "azure": "read access to the cost export in Blob Storage and the Reader role on the "
    "subscription or billing account",
    "digitalocean": "read-only access to the team's invoices, balance and resources",
}
# Who can do it (docs/adr/022: each from the provider's own documentation).
WHO: dict[str, str] = {
    "aws": "an IAM user or role that can create IAM roles and CloudFormation stacks",
    "gcp": "someone who can turn on the billing export, share BigQuery datasets and grant IAM "
    "roles (Billing Account Administrator, or Project IAM Admin and BigQuery Admin)",
    "azure": "an Owner, or a Contributor with User Access Administrator; consent for the "
    "whole organization needs a Privileged Role Administrator",
    "digitalocean": "a team Owner, or a Member who can approve apps",
}
NOT_GRANTED = (
    "It cannot change, start or delete anything, and cannot read files, databases or "
    "secrets. You do not join the workspace and do not see its costs."
)

# --- SQL ------------------------------------------------------------------------------------

LOOKUP = text("SELECT id, tenant_id FROM connect_invite_lookup(:link, :sess)")
LOAD = text(
    "SELECT i.id, i.tenant_id, i.provider, i.email::text AS email, i.invited_by, i.intent_id, "
    "i.connection_id, i.email_sent, i.created_at, i.expires_at, i.opened_at, i.connected_at, "
    "i.revoked_at, i.expiry_notified_at, i.expires_at <= now() AS expired, "
    "u.email AS sender_email, u.display_name AS sender_name, t.name AS workspace, "
    "EXISTS (SELECT 1 FROM membership m WHERE m.tenant_id = i.tenant_id "
    "AND m.user_id = i.invited_by AND m.role = 'admin') AS sender_is_admin "
    "FROM connect_invite i JOIN tenant t ON t.id = i.tenant_id "
    "LEFT JOIN app_user u ON u.id = i.invited_by WHERE i.id = :id"
)
LIST = text(
    "SELECT i.id, i.provider, i.email::text AS email, i.connection_id, i.email_sent, "
    "i.created_at, i.expires_at, i.opened_at, i.connected_at, i.revoked_at, "
    "i.expires_at <= now() AS expired, u.email AS sender_email "
    "FROM connect_invite i LEFT JOIN app_user u ON u.id = i.invited_by "
    "WHERE i.tenant_id = :t ORDER BY i.created_at DESC LIMIT 50"
)
UNSETTLED = text(
    "SELECT id FROM connect_invite WHERE tenant_id = :t AND connected_at IS NULL "
    "AND revoked_at IS NULL AND expiry_notified_at IS NULL"
)
SENT_TODAY = text(
    "SELECT count(*) AS n, min(created_at) AS oldest FROM connect_invite "
    "WHERE tenant_id = :t AND created_at > now() - interval '24 hours'"
)
LOCK_SENDS = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")
REPLACE_OPEN = text(
    "UPDATE connect_invite SET revoked_at = now(), session_hash = NULL WHERE tenant_id = :t "
    "AND provider = :p AND email = :e AND connected_at IS NULL AND revoked_at IS NULL "
    "RETURNING id"
)
INSERT = text(
    "INSERT INTO connect_invite (id, tenant_id, provider, email, token_hash, invited_by, "
    "expires_at) VALUES (:id, :t, :p, :e, :h, :by, :x)"
)
MARK_SENT = text("UPDATE connect_invite SET email_sent = :sent WHERE id = :id")
REVOKE = text(
    "UPDATE connect_invite SET revoked_at = now(), session_hash = NULL WHERE id = :id "
    "AND tenant_id = :t AND connected_at IS NULL AND revoked_at IS NULL RETURNING email::text"
)
EXISTS_IN_TENANT = text("SELECT connected_at FROM connect_invite WHERE id = :id AND tenant_id = :t")
OPEN_SESSION = text(
    "UPDATE connect_invite SET session_hash = :s, opened_at = coalesce(opened_at, now()) "
    "WHERE id = :id"
)
SET_INTENT = text("UPDATE connect_invite SET intent_id = :i WHERE id = :id")
SET_CONNECTION = text("UPDATE connect_invite SET connection_id = :c WHERE id = :id")
INTENT_CONNECTION = text("SELECT connection_id FROM connection_intent WHERE id = :i")
CONNECTION_STATUS = text("SELECT status FROM cloud_connection WHERE id = :c")
MARK_CONNECTED = text(
    "UPDATE connect_invite SET connected_at = now() WHERE id = :id AND connected_at IS NULL "
    "RETURNING connected_at"
)
MARK_EXPIRY_NOTIFIED = text(
    "UPDATE connect_invite SET expiry_notified_at = now() WHERE id = :id "
    "AND expiry_notified_at IS NULL AND connected_at IS NULL RETURNING id"
)
NOTIFIED = text(
    "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, status, "
    "error) VALUES (:id, :t, :kind, :k, :to, :subject, :status, :error) "
    "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING"
)
DUE = text(
    "SELECT tenant_id, id, invited_by FROM connect_invite WHERE connected_at IS NULL "
    "AND revoked_at IS NULL AND expiry_notified_at IS NULL AND (connection_id IS NOT NULL "
    "OR intent_id IS NOT NULL OR expires_at <= now()) LIMIT 500"
)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(b"cloudoptics/connect-invite:" + token.encode()).digest()


def invite_status(row: Any) -> InviteStatus:
    if row.revoked_at is not None:
        return "revoked"
    if row.connected_at is not None:
        return "connected"
    if row.expired:
        return "expired"
    if row.opened_at is not None:
        return "opened"
    return "sent"


# --- models ---------------------------------------------------------------------------------


class ConnectInviteCreate(ApiModel):
    email: EmailStr
    provider: ProviderName


class ConnectInvite(ApiModel):
    id: uuid.UUID
    provider: ProviderName
    email: str
    status: InviteStatus
    invited_by_email: str | None
    created_at: datetime
    expires_at: datetime
    opened_at: datetime | None
    connected_at: datetime | None
    revoked_at: datetime | None
    connection_id: uuid.UUID | None
    #: Whether the email went out (Postmark configured and it accepted the message).
    email_sent: bool


class InviteOpen(ApiModel):
    token: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class Requester(ApiModel):
    name: str | None
    email: str | None


class InviteView(ApiModel):
    """What the person holding the link sees: who asked, for which company and cloud, and the
    connection they set up (with its access check). No costs, members or settings."""

    provider: ProviderName
    email: str
    status: InviteStatus
    requested_by: Requester
    workspace_name: str
    expires_at: datetime
    grants: str
    who_can: str
    connection: Connection | None


class InviteSession(ApiModel):
    session_token: str
    invite: InviteView


def _model(row: Any) -> ConnectInvite:
    return ConnectInvite(
        id=row.id,
        provider=row.provider,
        email=row.email,
        status=invite_status(row),
        invited_by_email=row.sender_email,
        created_at=row.created_at,
        expires_at=row.expires_at,
        opened_at=row.opened_at,
        connected_at=row.connected_at,
        revoked_at=row.revoked_at,
        connection_id=row.connection_id,
        email_sent=row.email_sent,
    )


async def _load(session: AsyncSession, invite_id: uuid.UUID) -> Any:
    return (await session.execute(LOAD, {"id": invite_id})).one()


async def _connection(session: AsyncSession, connection_id: uuid.UUID | None) -> Connection | None:
    if connection_id is None:
        return None
    try:
        return connections._to_model(await get_connection(session, connection_id))
    except ConnectionNotFoundError:
        return None


async def _view(session: AsyncSession, row: Any) -> InviteView:
    return InviteView(
        provider=row.provider,
        email=row.email,
        status=invite_status(row),
        requested_by=Requester(name=row.sender_name, email=row.sender_email),
        workspace_name=row.workspace,
        expires_at=row.expires_at,
        grants=GRANTS[row.provider],
        who_can=WHO[row.provider],
        connection=await _connection(session, row.connection_id),
    )


# --- emails ---------------------------------------------------------------------------------


def invite_email(to: str, row: Any, token: str, sender: str) -> Email:
    cloud = provider_label(row.provider)
    link = f"{get_settings().app_url.rstrip('/')}/connect-invite/{token}"
    until = f"{row.expires_at:%d %B %Y}"
    subject = f"{sender} asked you to connect {row.workspace}'s {cloud} to Cloudoptics"
    lines = [
        f"{sender} asked you to connect {cloud} for {row.workspace} to Cloudoptics, a cloud "
        "cost tool. They do not have the access to do it themselves.",
        "",
        f"What it grants: {GRANTS[row.provider]}. Read-only.",
        f"What it does not: {NOT_GRANTED}",
        f"Who can do it: {WHO[row.provider]}.",
        "",
        f"Open the connect step: {link}",
        "",
        f"The link connects one {cloud} account and works until {until}. If you did not expect "
        "this, ignore it: nothing happens until someone opens the link and finishes the step.",
    ]
    body = (
        f"<p>{html.escape(sender)} asked you to connect {html.escape(cloud)} for "
        f"<strong>{html.escape(row.workspace)}</strong> to Cloudoptics, a cloud cost tool. "
        "They do not have the access to do it themselves.</p>"
        f"<p><strong>What it grants:</strong> {html.escape(GRANTS[row.provider])}. "
        "Read-only.<br>"
        f"<strong>What it does not:</strong> {html.escape(NOT_GRANTED)}<br>"
        f"<strong>Who can do it:</strong> {html.escape(WHO[row.provider])}.</p>"
        f'<p><a href="{html.escape(link)}">Open the connect step</a></p>'
        f"<p>The link connects one {html.escape(cloud)} account and works until {until}. If "
        "you did not expect this, ignore it: nothing happens until someone opens the link and "
        "finishes the step.</p>"
    )
    return Email([to], subject, "\n".join(lines), body, tag="connect-invite")


def outcome_email(row: Any, outcome: Literal["connected", "expired"]) -> Email:
    cloud = provider_label(row.provider)
    link = f"{get_settings().app_url.rstrip('/')}/settings/connections"
    if outcome == "connected":
        subject = f"{row.email} connected {cloud} for {row.workspace}"
        words = (
            f"{row.email} connected {cloud} for {row.workspace}. Cloudoptics is loading the "
            "bills now; the first costs show within the hour for most accounts."
        )
    else:
        subject = f"Your request to {row.email} to connect {cloud} expired"
        words = (
            f"The link you sent {row.email} to connect {cloud} for {row.workspace} expired "
            f"on {row.expires_at:%d %B %Y} without being used. Send a new one from the "
            "Connections page."
        )
    return Email(
        [row.sender_email],
        subject,
        f"{words}\n\nConnections: {link}",
        f'<p>{html.escape(words)}</p><p><a href="{html.escape(link)}">Open Connections</a></p>',
        tag=f"connect-invite-{outcome}",
    )


async def _notify(
    session: AsyncSession, row: Any, outcome: Literal["connected", "expired"]
) -> None:
    """Email the sender once per outcome (``notification`` de-duplicates)."""
    if not row.sender_email:
        return
    key = f"connect_invite:{row.id}:{outcome}"
    if (
        await session.execute(text("SELECT 1 FROM notification WHERE dedupe_key = :k"), {"k": key})
    ).first():
        return
    email = outcome_email(row, outcome)
    state, error = "sent", None
    try:
        await get_mailer().send(email)
    except EmailError as exc:
        state, error = "failed", str(exc)
        log.warning("connect invite %s email to its sender not sent: %s", row.id, exc)
    await session.execute(
        NOTIFIED,
        {
            "id": uuid7(),
            "t": row.tenant_id,
            "kind": f"connect_invite_{outcome}",
            "k": key,
            "to": email.to,
            "subject": email.subject,
            "status": state,
            "error": error,
        },
    )


# --- the state machine ----------------------------------------------------------------------


async def settle(session: AsyncSession, invite_id: uuid.UUID) -> Any:
    """Inside the invite's tenant scope: pick up the connection an AWS stack reported, mark
    the invite connected once that connection works, or note that it expired unused. Each
    outcome is audited and emailed to the sender once. Returns the invite as it now is."""
    row = await _load(session, invite_id)
    if row.connected_at is not None or row.revoked_at is not None:
        return row
    connection_id = row.connection_id
    if connection_id is None and row.intent_id is not None:
        connection_id = (await session.execute(INTENT_CONNECTION, {"i": row.intent_id})).scalar()
        if connection_id is not None:
            await session.execute(SET_CONNECTION, {"c": connection_id, "id": row.id})
    if connection_id is not None:
        state = (await session.execute(CONNECTION_STATUS, {"c": connection_id})).scalar()
        if (
            state in accounts.USABLE
            and (await session.execute(MARK_CONNECTED, {"id": row.id})).first()
        ):
            row = await _load(session, invite_id)
            await audit.record(
                session,
                tenant_id=row.tenant_id,
                actor=Actor(type="connect_invite", id=row.id),
                action="connect_invite.connected",
                target_type="connect_invite",
                target_id=str(row.id),
                after={"provider": row.provider, "connectionId": str(connection_id)},
            )
            await _notify(session, row, "connected")
            return row
    if row.expired and (await session.execute(MARK_EXPIRY_NOTIFIED, {"id": row.id})).first():
        await audit.record(
            session,
            tenant_id=row.tenant_id,
            actor=Actor(type="system"),
            action="connect_invite.expired",
            target_type="connect_invite",
            target_id=str(row.id),
            after={"provider": row.provider, "email": row.email},
        )
        await _notify(session, row, "expired")
    return await _load(session, invite_id)


# --- sender routes --------------------------------------------------------------------------


def _too_many(oldest: datetime) -> ApiProblem:
    wait = max(60, int((oldest + timedelta(hours=24) - datetime.now(UTC)).total_seconds()))
    hours = max(1, round(wait / 3600))
    limit = get_settings().connect_invites_per_day
    return ApiProblem(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limited",
        f"This workspace sent {limit} connection requests in the last 24 hours. "
        f"Try again in about {hours} hour{'s' if hours != 1 else ''}.",
        headers={"Retry-After": str(wait)},
    )


@router.post("", response_model=ConnectInvite, status_code=status.HTTP_201_CREATED)
async def send_invite(
    body: ConnectInviteCreate, request: Request, access: AdminAccess, session: Session
) -> ConnectInvite:
    email = str(body.email).strip().lower()
    # One sender at a time per workspace, so the daily count cannot be raced past.
    await session.execute(LOCK_SENDS, {"k": f"connect_invite:{access.tenant_id}"})
    sent = (await session.execute(SENT_TODAY, {"t": access.tenant_id})).one()
    if sent.n >= get_settings().connect_invites_per_day:
        raise _too_many(sent.oldest)
    actor = user_actor(request, access.user.id)
    # Sending the same cloud to the same address again replaces the open request.
    for (old,) in (
        await session.execute(REPLACE_OPEN, {"t": access.tenant_id, "p": body.provider, "e": email})
    ).all():
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=actor,
            action="connect_invite.revoked",
            target_type="connect_invite",
            target_id=str(old),
            after={"reason": "replaced"},
        )
    token = secrets.token_urlsafe(32)
    invite_id = uuid7()
    await session.execute(
        INSERT,
        {
            "id": invite_id,
            "t": access.tenant_id,
            "p": body.provider,
            "e": email,
            "h": token_hash(token),
            "by": access.user.id,
            "x": datetime.now(UTC) + timedelta(days=INVITE_DAYS),
        },
    )
    row = await _load(session, invite_id)
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=actor,
        action="connect_invite.sent",
        target_type="connect_invite",
        target_id=str(invite_id),
        after={"provider": body.provider, "email": email, "expiresAt": row.expires_at.isoformat()},
    )
    ok = True
    try:
        sender = access.user.display_name or access.user.email
        await get_mailer().send(invite_email(email, row, token, sender))
    except EmailError as exc:
        ok = False
        log.warning("connect invite email for tenant %s not sent: %s", access.tenant_id, exc)
    del token
    await session.execute(MARK_SENT, {"sent": ok, "id": invite_id})
    return _model(await _load(session, invite_id))


@router.get("", response_model=list[ConnectInvite])
async def list_invites(access: AdminAccess, session: Session) -> list[ConnectInvite]:
    """Newest first, each settled first, so a connection or an expiry shows at once."""
    for (invite_id,) in (await session.execute(UNSETTLED, {"t": access.tenant_id})).all():
        await settle(session, invite_id)
    rows = await session.execute(LIST, {"t": access.tenant_id})
    return [_model(r) for r in rows]


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> Response:
    email = (await session.execute(REVOKE, {"id": invite_id, "t": access.tenant_id})).scalar()
    if email is None:
        found = (
            await session.execute(EXISTS_IN_TENANT, {"id": invite_id, "t": access.tenant_id})
        ).one_or_none()
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
        if found.connected_at is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This request already connected an account. Disconnect the connection instead.",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)  # already revoked
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="connect_invite.revoked",
        target_type="connect_invite",
        target_id=str(invite_id),
        before={"email": email},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- the invite session ---------------------------------------------------------------------

REVOKED = "This link was withdrawn. Ask the person who sent it for a new one."
EXPIRED = "This link has expired. Ask the person who sent it for a new one."
USED = "This link already connected an account. You can close this page."
SENDER_GONE = (
    "The person who sent this link is no longer an admin of the workspace, so it no longer "
    "works. Ask an admin of the workspace for a new one."
)
UNKNOWN = "This link is not valid. Open the newest link from your email, or ask for a new one."


def _session_required() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Open the link from your email to continue.",
        headers={"WWW-Authenticate": "ConnectInvite"},
    )


def _refusal(row: Any) -> ApiProblem | None:
    if row.revoked_at is not None:
        return ApiProblem(status.HTTP_410_GONE, "invite_revoked", REVOKED)
    if row.connected_at is None and row.expired:
        return ApiProblem(status.HTTP_410_GONE, "invite_expired", EXPIRED)
    if not row.sender_is_admin:
        return ApiProblem(status.HTTP_403_FORBIDDEN, "invite_sender_not_admin", SENDER_GONE)
    return None


@dataclass(frozen=True)
class InviteAccess:
    """A connect-invite session: the invite, and the access its handlers run with."""

    invite: Any
    access: TenantAccess

    @property
    def connected(self) -> bool:
        return self.invite.connected_at is not None


async def _enter(request: Request, session: AsyncSession, found: Any) -> InviteAccess:
    await scope(session, tenant_id=found.tenant_id)
    row = await _load(session, found.id)
    problem = _refusal(row)
    if problem is not None:
        raise problem
    await scope(session, tenant_id=row.tenant_id, user_id=row.invited_by)
    await ratelimit.per_tenant(row.tenant_id)
    request.state.connect_invite_id = row.id
    sender = CurrentUser(
        id=row.invited_by,
        email=row.sender_email or "",
        display_name=row.sender_name,
        email_verified=True,
    )
    return InviteAccess(
        invite=row,
        access=TenantAccess(
            tenant_id=row.tenant_id, user=sender, role="admin", connect_invite_id=row.id
        ),
    )


async def invite_session(request: Request, session: Session) -> InviteAccess:
    """The only dependency that accepts a connect-invite session (ADR-022)."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != SESSION_SCHEME or not token.strip():
        raise _session_required()
    found = (
        await session.execute(LOOKUP, {"link": None, "sess": token_hash(token.strip())})
    ).one_or_none()
    if found is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "This page was opened from an older link, or the link was opened again elsewhere. "
            "Open the newest link from your email.",
            headers={"WWW-Authenticate": "ConnectInvite"},
        )
    return await _enter(request, session, found)


Invited = Annotated[InviteAccess, Depends(invite_session)]


def _provider(invited: InviteAccess, provider: str) -> None:
    if invited.invite.provider != provider:
        cloud = provider_label(invited.invite.provider)
        raise ApiProblem(
            status.HTTP_403_FORBIDDEN, "invite_provider", f"This link is for {cloud} only."
        )


async def _may_connect(session: AsyncSession, invited: InviteAccess, provider: str) -> None:
    """Connect actions: the invite's own cloud, not after it connected, and not a second
    connection while the first is still being set up."""
    _provider(invited, provider)
    if invited.connected:
        raise ApiProblem(status.HTTP_409_CONFLICT, "invite_used", USED)
    current = await _connection(session, invited.invite.connection_id)
    if current is not None and current.status != "disconnected":
        raise ApiProblem(
            status.HTTP_409_CONFLICT,
            "invite_in_progress",
            "This link already started a connection. Finish it below, or run Check access.",
        )


def _own_connection(invited: InviteAccess) -> uuid.UUID:
    if invited.invite.connection_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nothing is connected through this link yet")
    return uuid.UUID(str(invited.invite.connection_id))


async def _link(session: AsyncSession, invited: InviteAccess, connection_id: uuid.UUID) -> None:
    await session.execute(SET_CONNECTION, {"c": connection_id, "id": invited.invite.id})


@invite_router.post("/open", response_model=InviteSession)
async def open_invite(body: InviteOpen, request: Request, session: Session) -> InviteSession:
    """The link from the email: a new session (the previous one, if any, ends)."""
    found = (
        await session.execute(LOOKUP, {"link": token_hash(body.token), "sess": None})
    ).one_or_none()
    if found is None:
        raise ApiProblem(status.HTTP_404_NOT_FOUND, "invite_unknown", UNKNOWN)
    invited = await _enter(request, session, found)
    row = invited.invite
    if row.connected_at is not None:
        raise ApiProblem(status.HTTP_410_GONE, "invite_used", USED)
    session_token = secrets.token_urlsafe(32)
    await session.execute(OPEN_SESSION, {"s": token_hash(session_token), "id": row.id})
    await audit.record(
        session,
        tenant_id=row.tenant_id,
        actor=user_actor(request, row.invited_by),
        action="connect_invite.opened",
        target_type="connect_invite",
        target_id=str(row.id),
        after={"first": row.opened_at is None},
    )
    return InviteSession(
        session_token=session_token, invite=await _view(session, await settle(session, row.id))
    )


@invite_router.get("/session", response_model=InviteView)
async def read_session(invited: Invited, session: Session) -> InviteView:
    """Who asked, the connect step's cloud, and the connection with its access check."""
    return await _view(session, await settle(session, invited.invite.id))


@invite_router.post(
    "/session/check",
    response_model=Connection | ValidationAccepted,
    responses={202: {"model": ValidationAccepted}},
)
async def check_access(
    invited: Invited, request: Request, response: Response, session: Session
) -> Connection | ValidationAccepted:
    """Check access on the connection this link set up (also after it connected)."""
    connection_id = _own_connection(invited)
    if invited.invite.provider == "azure":
        await azure.validate_connection(connection_id, request, invited.access, session)
        result: Connection | ValidationAccepted = connections._to_model(
            await get_connection(session, connection_id)
        )
    else:
        result = await connections.validate(
            connection_id, request, response, invited.access, session
        )
    await settle(session, invited.invite.id)
    return result


# AWS: the one-click link (a connection intent made for the sender's workspace).


@invite_router.post(
    "/session/aws/link", response_model=intents.IntentSetup, status_code=status.HTTP_201_CREATED
)
async def aws_link(invited: Invited, session: Session) -> intents.IntentSetup:
    await _may_connect(session, invited, "aws")
    setup = await intents.create_intent(invited.access, session)
    await session.execute(SET_INTENT, {"i": setup.intent.id, "id": invited.invite.id})
    return setup


@invite_router.get("/session/aws/link/{intent_id}", response_model=intents.Intent)
async def aws_link_status(
    intent_id: uuid.UUID, invited: Invited, session: Session
) -> intents.Intent:
    _provider(invited, "aws")
    if invited.invite.intent_id is None or str(invited.invite.intent_id) != str(intent_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    intent = await intents.get_intent(intent_id, invited.access, session)
    await settle(session, invited.invite.id)
    return intent


# Google Cloud: the export's location, then the steps with this connection's own account.


@invite_router.post(
    "/session/gcp", response_model=gcp.GcpSetup, status_code=status.HTTP_201_CREATED
)
async def gcp_create(
    body: gcp.GcpConnectionCreate, invited: Invited, request: Request, session: Session
) -> gcp.GcpSetup:
    await _may_connect(session, invited, "gcp")
    setup = await gcp.create_gcp_connection(body, request, invited.access, session)
    await _link(session, invited, setup.connection.id)
    return setup


@invite_router.get("/session/gcp", response_model=gcp.GcpSetup)
async def gcp_setup(invited: Invited, request: Request, session: Session) -> gcp.GcpSetup:
    _provider(invited, "gcp")
    return await gcp.get_gcp_setup(_own_connection(invited), request, invited.access, session)


# Azure: the export's location, then consent and the role assignment.


@invite_router.post(
    "/session/azure",
    response_model=azure.AzureConnectionSetup,
    status_code=status.HTTP_201_CREATED,
)
async def azure_create(
    body: azure.AzureConnectionCreate, invited: Invited, request: Request, session: Session
) -> azure.AzureConnectionSetup:
    await _may_connect(session, invited, "azure")
    setup = await azure.create_connection(body, request, invited.access, session)
    await _link(session, invited, setup.connection.id)
    return setup


@invite_router.get("/session/azure", response_model=azure.AzureConnectionSetup)
async def azure_setup(invited: Invited, session: Session) -> azure.AzureConnectionSetup:
    _provider(invited, "azure")
    return await azure.get_setup(_own_connection(invited), invited.access, session)


# DigitalOcean: sign in and approve (bound to this invite), or paste a read-only token.


@invite_router.post(
    "/session/digitalocean", response_model=Connection, status_code=status.HTTP_201_CREATED
)
async def digitalocean_token(
    body: digitalocean.TokenBody, invited: Invited, request: Request, session: Session
) -> Connection:
    await _may_connect(session, invited, "digitalocean")
    conn = await digitalocean.connect_digitalocean(body, request, invited.access, session)
    await _link(session, invited, conn.id)
    await settle(session, invited.invite.id)
    return conn


@invite_router.get("/session/digitalocean/oauth", response_model=digitalocean.OAuthConfig)
async def digitalocean_oauth(invited: Invited) -> digitalocean.OAuthConfig:
    _provider(invited, "digitalocean")
    return await digitalocean.oauth_config(invited.access)


@invite_router.post(
    "/session/digitalocean/oauth/start",
    response_model=digitalocean.OAuthStart,
    status_code=status.HTTP_201_CREATED,
)
async def digitalocean_oauth_start(invited: Invited, session: Session) -> digitalocean.OAuthStart:
    await _may_connect(session, invited, "digitalocean")
    return await digitalocean.oauth_start(invited.access, session)


@invite_router.post(
    "/session/digitalocean/oauth/callback",
    response_model=Connection,
    status_code=status.HTTP_201_CREATED,
)
async def digitalocean_oauth_callback(
    body: digitalocean.OAuthCallback, invited: Invited, request: Request, session: Session
) -> Connection:
    await _may_connect(session, invited, "digitalocean")
    conn = await digitalocean.oauth_callback(body, request, invited.access, session)
    await _link(session, invited, conn.id)
    await settle(session, invited.invite.id)
    return conn


# --- the sweep (connectors:connect_invites.sweep, every 15 minutes) -------------------------


async def sweep() -> int:
    """Settle every invite that may have connected or expired. Returns how many changed."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        due = (await session.execute(DUE)).all()
    changed = 0
    for tenant_id, invite_id, sender in due:
        async with get_sessionmaker()() as session, session.begin():
            await scope(session, tenant_id=tenant_id, user_id=sender)
            before = await _load(session, invite_id)
            after = await settle(session, invite_id)
            changed += int(
                (before.connected_at, before.expiry_notified_at)
                != (after.connected_at, after.expiry_notified_at)
            )
    return changed
