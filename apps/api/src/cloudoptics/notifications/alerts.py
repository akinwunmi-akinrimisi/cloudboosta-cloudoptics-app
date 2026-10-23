"""Connection alerts (PRD FR-NOT-02 "connection failure"): email the workspace admins when
a connection that was working gets worse.

An alert goes out when a validation moves a connection's health from healthy to degraded
or failed, or from degraded to failed; never for a connection that has not yet worked
(the admin is setting it up and sees the result on screen). At most one email per
connection, health and day (FR-NOT-04).
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

RANK = {"healthy": 0, "degraded": 1, "failed": 2}


def should_alert(previous: str | None, current: str) -> bool:
    if previous is None or previous not in RANK or current not in RANK:
        return False
    return RANK[current] > RANK[previous]


def connection_email(conn: Any, checks: list[dict[str, Any]], app_url: str) -> tuple[str, str, str]:
    """Subject, text and HTML for a connection that stopped working."""
    account = conn.aws_account_id
    worse = "has stopped working" if conn.health == "failed" else "needs attention"
    subject = f"Action needed: your AWS connection {account} {worse}"
    problems = [c for c in checks if c.get("status") in ("error", "warning", "failed")]
    link = f"{app_url.rstrip('/')}/settings/connections"
    lines = [
        f"Cloudoptics can no longer fully read AWS account {account}.",
        "",
        *[f"- {c.get('label')}: {c.get('hint') or c.get('message')}" for c in problems],
        "",
        "Until this is fixed, costs and recommendations for this account may be out of date.",
        f"Review the connection and run the check again: {link}",
    ]
    items = "".join(
        f"<li><strong>{html.escape(str(c.get('label')))}</strong>: "
        f"{html.escape(str(c.get('hint') or c.get('message')))}</li>"
        for c in problems
    )
    body = (
        f"<p>Cloudoptics can no longer fully read AWS account <strong>{html.escape(account)}"
        f"</strong>.</p>{f'<ul>{items}</ul>' if items else ''}"
        "<p>Until this is fixed, costs and recommendations for this account may be out of "
        f'date.</p><p><a href="{html.escape(link)}">Review the connection</a> and run the '
        "check again.</p>"
    )
    return subject, "\n".join(lines), body


async def send_connection_alert(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> str:
    """Inside a tenant-scoped transaction. Returns sent, failed or skipped."""
    conn = (
        await session.execute(
            text(
                "SELECT aws_account_id, health, permission_checks FROM cloud_connection "
                "WHERE id = :c"
            ),
            {"c": connection_id},
        )
    ).one_or_none()
    if conn is None or conn.health not in ("degraded", "failed"):
        return "skipped"
    today = datetime.now(UTC).date().isoformat()
    key = f"connection:{connection_id}:{conn.health}:{today}"
    exists = (
        await session.execute(text("SELECT 1 FROM notification WHERE dedupe_key = :k"), {"k": key})
    ).first()
    if exists:
        return "skipped"
    recipients = [
        r.email
        for r in await session.execute(
            text(
                "SELECT u.email FROM membership m JOIN app_user u ON u.id = m.user_id "
                "WHERE m.tenant_id = :t AND m.role = 'admin' ORDER BY u.email"
            ),
            {"t": tenant_id},
        )
    ]
    subject, body_text, body_html = connection_email(
        conn, conn.permission_checks or [], get_settings().app_url
    )
    status, error = "skipped", None
    if recipients:
        try:
            await get_mailer().send(
                Email(recipients, subject, body_text, body_html, tag="connection-alert")
            )
            status = "sent"
        except EmailError as exc:
            status, error = "failed", str(exc)
            log.warning("connection alert for %s not sent: %s", connection_id, exc)
    await session.execute(
        text(
            "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, "
            "status, error) VALUES (:id, :t, 'connection_alert', :k, :to, :subject, :status, "
            ":error) ON CONFLICT (tenant_id, dedupe_key) DO NOTHING"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "k": key,
            "to": recipients,
            "subject": subject,
            "status": status,
            "error": error,
        },
    )
    return status
