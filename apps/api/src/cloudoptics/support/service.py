"""Break-glass grants (PRD FR-SEC-07, docs/10 T11, docs/11b §7.4).

A Cloudoptics staff member (``CLOUDOPTICS_PLATFORM_ADMIN_EMAILS``, verified email, MFA)
asks for read-only access to one workspace with a written justification. Access starts at
once; the workspace's admins are emailed and the workspace's own audit log shows who, why,
when and for how long. No tenant approval step: neither the PRD nor docs/10/11b asks for
one, and support is often needed when the admins cannot help (the tenant can see every use).

While a grant is active, ``identity.deps.require_role`` lets the staff member through
viewer routes of that workspace only, for safe methods only, and makes the request's
transaction read-only. Each such request (and each refused write) is written to
``support_access_request`` on its own connection before the route runs, so the record is
kept whatever the request does; if it cannot be written, the request is refused.
"""

from __future__ import annotations

import html
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.db import get_engine
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

DEFAULT_MINUTES = 60
MIN_MINUTES = 15
MAX_MINUTES = 120  # docs/11b §7.4: at most 2 hours
# Methods a grant allows: reads only, whatever role a route asks for.
SAFE_METHODS = frozenset({"GET", "HEAD"})

ACTIVE_GRANT = text(
    "SELECT id, expires_at FROM support_access_grant "
    "WHERE tenant_id = :t AND staff_user_id = :u AND ended_at IS NULL AND expires_at > now() "
    "ORDER BY expires_at DESC LIMIT 1"
)
OWN_ACTIVE_GRANTS = text(
    "SELECT g.id, g.tenant_id, g.tenant_name, g.expires_at FROM support_access_grant g "
    "WHERE g.staff_user_id = :u AND g.ended_at IS NULL AND g.expires_at > now() "
    "AND NOT EXISTS (SELECT 1 FROM membership m WHERE m.tenant_id = g.tenant_id "
    "AND m.user_id = :u) ORDER BY g.tenant_name"
)
READ_ONLY = text("SET TRANSACTION READ ONLY")
CONSOLE_SCOPE = text("SELECT set_config('app.support_console', 'on', true)")
TENANT_SCOPE = text("SELECT set_config('app.tenant_id', :t, true)")
LOG_REQUEST = text(
    "INSERT INTO support_access_request (grant_id, tenant_id, method, path, allowed) "
    "VALUES (:g, :t, :method, :path, :allowed)"
)
TENANT = text("SELECT name FROM tenant WHERE id = :t")
ADMIN_EMAILS = text(
    "SELECT u.email FROM membership m JOIN app_user u ON u.id = m.user_id "
    "WHERE m.tenant_id = :t AND m.role = 'admin' ORDER BY u.email"
)
INSERT_GRANT = text(
    "INSERT INTO support_access_grant (id, tenant_id, tenant_name, staff_user_id, "
    "justification, ticket_ref, expires_at) VALUES (:id, :t, :name, :u, :why, :ticket, "
    "now() + make_interval(mins => :minutes)) RETURNING id"
)
LIST_GRANTS = text(
    "SELECT g.id, g.tenant_id, g.tenant_name, g.staff_user_id, u.email AS staff_email, "
    "u.display_name AS staff_name, g.justification, g.ticket_ref, g.created_at, "
    "g.expires_at, g.ended_at, e.email AS ended_by_email, "
    "CASE WHEN g.ended_at IS NOT NULL THEN 'ended' WHEN g.expires_at <= now() THEN 'expired' "
    "ELSE 'active' END AS status, "
    "(SELECT count(*) FROM support_access_request r WHERE r.grant_id = g.id) AS requests "
    "FROM support_access_grant g JOIN app_user u ON u.id = g.staff_user_id "
    "LEFT JOIN app_user e ON e.id = g.ended_by "
    "ORDER BY g.created_at DESC LIMIT :n"
)
ONE_GRANT = text(
    "SELECT g.id, g.tenant_id, g.tenant_name, g.staff_user_id, u.email AS staff_email, "
    "u.display_name AS staff_name, g.justification, g.ticket_ref, g.created_at, "
    "g.expires_at, g.ended_at, e.email AS ended_by_email, "
    "CASE WHEN g.ended_at IS NOT NULL THEN 'ended' WHEN g.expires_at <= now() THEN 'expired' "
    "ELSE 'active' END AS status, "
    "(SELECT count(*) FROM support_access_request r WHERE r.grant_id = g.id) AS requests "
    "FROM support_access_grant g JOIN app_user u ON u.id = g.staff_user_id "
    "LEFT JOIN app_user e ON e.id = g.ended_by "
    "WHERE g.id = :id"
)
END_GRANT = text(
    "UPDATE support_access_grant SET ended_at = now(), ended_by = :u "
    "WHERE id = :id AND ended_at IS NULL AND expires_at > now() RETURNING id"
)
RECORD_NOTIFICATION = text(
    "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, status, "
    "error) VALUES (:id, :t, 'support_access', :k, :to, :subject, :status, :error) "
    "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING"
)


@dataclass(frozen=True)
class ActiveGrant:
    id: uuid.UUID
    expires_at: datetime


async def active_grant(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> ActiveGrant | None:
    """The user's active grant for this workspace (the session is scoped to it)."""
    row = (await session.execute(ACTIVE_GRANT, {"t": tenant_id, "u": user_id})).one_or_none()
    return ActiveGrant(row.id, row.expires_at) if row else None


async def record_request(
    grant_id: uuid.UUID, tenant_id: uuid.UUID, method: str, path: str, *, allowed: bool
) -> None:
    """Write one request under a grant on its own short transaction (kept even if the
    request fails or is refused). Raises 503 if it cannot be written: no unrecorded access."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(TENANT_SCOPE, {"t": str(tenant_id)})
            await conn.execute(
                LOG_REQUEST,
                {
                    "g": grant_id,
                    "t": tenant_id,
                    "method": method,
                    "path": path[:500],
                    "allowed": allowed,
                },
            )
            await conn.commit()
    except (SQLAlchemyError, OSError) as exc:
        log.error("support access request not recorded for grant %s: %s", grant_id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not record support access; try again"
        ) from exc


async def make_read_only(session: AsyncSession) -> None:
    """The rest of the request's transaction cannot write (PostgreSQL enforces it)."""
    await session.execute(READ_ONLY)


async def console_scope(session: AsyncSession) -> None:
    """Let the staff console read every grant (after the staff and MFA checks)."""
    await session.execute(CONSOLE_SCOPE)


def granted_email(
    to: list[str],
    workspace: str,
    staff: str,
    justification: str,
    ticket: str,
    expires_at: datetime,
) -> Email:
    link = f"{get_settings().app_url.rstrip('/')}/settings?section=security"
    until = expires_at.strftime("%d %B %Y %H:%M UTC")
    lines = [
        f"A member of Cloudoptics support, {staff}, opened read-only access to your "
        f"workspace {workspace}.",
        f"Reason: {justification}",
        f"Support ticket: {ticket}",
        f"The access ends automatically at {until}. Support can view your workspace but "
        "cannot change anything in it or in your AWS accounts.",
        f"Every use is in your workspace's audit log: {link}",
        "If you did not expect this, reply to this email or contact Cloudoptics support.",
    ]
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return Email(
        to,
        f"Cloudoptics support accessed your workspace {workspace}",
        "\n\n".join(lines),
        body,
        tag="support-access",
    )


async def notify_admins(session: AsyncSession, tenant_id: uuid.UUID, email: Email) -> str:
    """Send the email to the workspace admins and record it in the email log."""
    sent, error = "skipped", None
    if email.to:
        try:
            await get_mailer().send(email)
            sent = "sent"
        except EmailError as exc:
            sent, error = "failed", str(exc)
            log.warning("support access email not sent: %s", exc)
    await session.execute(
        RECORD_NOTIFICATION,
        {
            "id": uuid7(),
            "t": tenant_id,
            "k": f"support:{uuid7()}",
            "to": email.to,
            "subject": email.subject,
            "status": sent,
            "error": error,
        },
    )
    return sent
