"""Email the member a recommendation is assigned to (PRD US-5.1).

One email per assignment (a bulk assignment sends one email listing them all), never to
someone who assigned it to themselves. Assignment emails are part of the workflow, so
they are not among the optional emails a member can turn off.
"""

from __future__ import annotations

import html
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.naming import where

log = logging.getLogger(__name__)

MAX_LISTED = 20


def assignment_email(by: str, recs: Sequence[Any], app_url: str) -> tuple[str, str, str]:
    """Subject, text and HTML."""
    base = app_url.rstrip("/")
    n = len(recs)
    subject = (
        f"{by} assigned you a recommendation: {recs[0].title}"
        if n == 1
        else f"{by} assigned you {n} recommendations"
    )
    shown = recs[:MAX_LISTED]
    lines = [f"{by} assigned you {'this recommendation' if n == 1 else 'these recommendations'}"
             " in Cloudoptics:", ""]  # fmt: skip
    items = []
    for r in shown:
        link = f"{base}/recommendations/{r.id}"
        saving = f"USD {float(r.savings_usd_month):,.2f} a month"
        lines.append(f"- {r.title} ({saving}): {link}")
        items.append(
            f'<li><a href="{html.escape(link)}">{html.escape(r.title)}</a> '
            f"({html.escape(saving)})</li>"
        )
    if n > len(shown):
        lines.append(f"... and {n - len(shown)} more: {base}/recommendations")
        items.append(f"<li>and {n - len(shown)} more</li>")
    # "in AWS", "in Azure", or "in your cloud accounts" for recommendations across clouds.
    cloud = where(getattr(r, "provider", None) for r in recs)
    lines += [
        "",
        f"Mark each one done when the change is made; Cloudoptics then checks it in {cloud}.",
    ]
    body = (
        f"<p>{html.escape(by)} assigned you "
        f"{'this recommendation' if n == 1 else 'these recommendations'} in Cloudoptics:</p>"
        f"<ul>{''.join(items)}</ul><p>Mark each one done when the change is made; Cloudoptics "
        f"then checks it in {html.escape(cloud)}.</p>"
    )
    return subject, "\n".join(lines), body


async def send_assignment_email(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    assignee_id: uuid.UUID,
    actor_id: uuid.UUID,
    recommendation_ids: list[uuid.UUID],
    key: str,
) -> str:
    """Inside a tenant-scoped transaction. Returns sent, failed or skipped."""
    if assignee_id == actor_id or not recommendation_ids:
        return "skipped"
    dedupe = f"assigned:{key}"
    exists = (
        await session.execute(
            text("SELECT 1 FROM notification WHERE dedupe_key = :k"), {"k": dedupe}
        )
    ).first()
    if exists:
        return "skipped"
    people = {
        r.id: r
        for r in await session.execute(
            text(
                "SELECT u.id, u.email, coalesce(u.display_name, u.email::text) AS name "
                "FROM app_user u JOIN membership m ON m.user_id = u.id AND m.tenant_id = :t "
                "WHERE u.id = ANY(:ids)"
            ),
            {"t": tenant_id, "ids": [assignee_id, actor_id]},
        )
    }
    recs = (
        await session.execute(
            text(
                "SELECT id, title, savings_usd_month, provider FROM recommendation "
                "WHERE id = ANY(:ids) "
                "AND assignee_id = :a ORDER BY priority_score DESC, id"
            ),
            {"ids": recommendation_ids, "a": assignee_id},
        )
    ).all()
    assignee = people.get(assignee_id)
    if assignee is None or not recs:
        return "skipped"
    by = people[actor_id].name if actor_id in people else "A teammate"
    subject, body_text, body_html = assignment_email(by, recs, get_settings().app_url)
    status, error = "sent", None
    try:
        await get_mailer().send(
            Email([assignee.email.lower()], subject, body_text, body_html, tag="assignment")
        )
    except EmailError as exc:
        status, error = "failed", str(exc)
        log.warning("assignment email not sent: %s", exc)
    await session.execute(
        text(
            "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, "
            "status, error) VALUES (:id, :t, 'recommendation_assigned', :k, :to, :subject, "
            ":status, :error) ON CONFLICT (tenant_id, dedupe_key) DO NOTHING"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "k": dedupe,
            "to": [assignee.email.lower()],
            "subject": subject,
            "status": status,
            "error": error,
        },
    )
    return status
